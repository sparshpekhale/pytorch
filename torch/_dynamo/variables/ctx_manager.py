import inspect
from typing import Dict, List

import torch._C
from torch._guards import Guard

from .. import variables
from ..bytecode_transformation import create_call_function, create_instruction
from ..device_interface import get_interface_for_device
from ..exc import unimplemented, Unsupported
from ..guards import GuardBuilder
from ..source import AttrSource, GlobalStateSource
from .base import VariableTracker
from .functions import (
    NestedUserFunctionVariable,
    UserFunctionVariable,
    UserMethodVariable,
    WrappedUserFunctionVariable,
    WrappedUserMethodVariable,
)


class ContextWrappingVariable(VariableTracker):
    def __init__(self, target_values, initial_values=None, **kwargs):
        super().__init__(**kwargs)
        self.target_values = target_values
        self.initial_values = initial_values

    def enter(self, tx):
        self._call_func(tx, self.target_values)
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def exit(self, tx, *args):
        self._call_func(tx, self.initial_values)
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def reconstruct(self, codegen):
        attr_source = AttrSource(
            codegen.tx.import_source(self.module_name()), self.fn_name()
        )
        return attr_source.reconstruct(codegen)

    def module_name(self):
        raise NotImplementedError("module_name called on base")

    def fn_name(self):
        raise NotImplementedError("fn_name called on base")

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        assert len(args) == 1
        if isinstance(args[0], NestedUserFunctionVariable):
            args[0] = UserFunctionVariable(args[0].get_function())
        assert isinstance(args[0], (UserMethodVariable, UserFunctionVariable))

        if isinstance(args[0], UserMethodVariable):
            return WrappedUserMethodVariable(args[0], self)

        if isinstance(args[0], UserFunctionVariable):
            return WrappedUserFunctionVariable(args[0], self)


class GenericContextWrappingVariable(ContextWrappingVariable):
    def __init__(self, target_values, initial_values=None, **kwargs):
        cm_obj = kwargs.pop("cm_obj", None)
        assert cm_obj is not None
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.cm_obj = cm_obj

    def enter(self, tx):
        options = VariableTracker.propagate(self)
        options["source"] = (
            None if self.source is None else AttrSource(self.source, "__enter__")
        )
        try:
            return variables.UserMethodVariable(
                self.cm_obj.__enter__.__func__,
                variables.UserDefinedObjectVariable(self.cm_obj, **options),
                **options,
            ).call_function(tx, [], {})
        except Unsupported as e:
            raise unimplemented(
                f"Unsupported context manager {self.cm_obj}'s __enter__ function"
            ) from e

    def exit(self, tx, *args):
        options = VariableTracker.propagate(self)
        options["source"] = (
            None if self.source is None else AttrSource(self.source, "__exit__")
        )
        try:
            x = variables.UserMethodVariable(
                self.cm_obj.__exit__.__func__,
                variables.UserDefinedObjectVariable(self.cm_obj, **options),
                **options,
            ).call_function(
                tx,
                [
                    variables.ConstantVariable.create(None),
                    variables.ConstantVariable.create(None),
                    variables.ConstantVariable.create(None),
                ],
                {},
            )
        except Unsupported as e:
            raise unimplemented(
                f"Unsupported context manager {self.cm_obj}'s __exit__ function"
            ) from e

        # Remove the checkpoint if there is no graph break
        # under this GenericContextWrappingVariable.
        tx.states_before_block.pop()
        return x


class GradModeVariable(ContextWrappingVariable):
    """represents torch.{no_grad,enable_grad,set_grad_mode}()"""

    _guards_singleton = {Guard(GlobalStateSource(), GuardBuilder.GRAD_MODE)}

    @staticmethod
    def create(tx, target_value, initialized=True, **kwargs):
        var = GradModeVariable(
            target_values=[target_value],
            initial_values=[torch.is_grad_enabled()],
            initialized=initialized,
            **kwargs,
        )
        if var.initialized:
            var._call_func(tx, var.target_values)
        return var

    def __init__(self, target_values, initial_values=None, initialized=True, **kwargs):
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.guards = self.guards | self._guards_singleton
        self.initialized = initialized

    def enter(self, tx):
        if not self.initialized:
            self._call_func(tx, self.target_values)
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def _call_func(self, tx, values):
        assert len(values) == 1
        value = values[0]
        tx.output.create_node(
            "call_function", torch._C._set_grad_enabled, (value,), {}
        ),
        torch._C._set_grad_enabled(value)

    def module_name(self):
        return "torch"

    def fn_name(self):
        return "set_grad_enabled"


class InferenceModeVariable(ContextWrappingVariable):
    @staticmethod
    def create(tx, target_values, **kwargs):
        var = InferenceModeVariable(
            target_values, initial_values=torch.is_inference_mode_enabled(), **kwargs
        )
        return var

    def __init__(
        self, target_values, initial_values=torch.is_inference_mode_enabled(), **kwargs
    ):
        mode = kwargs.pop("mode", None)
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.target_values = target_values
        self.mode = mode

    def exit(self, tx, *args):
        self.mode = (
            torch.autograd.grad_mode._exit_inference_mode(self.mode[0]),
            tx.output.create_node(
                "call_function",
                torch.autograd.grad_mode._exit_inference_mode,
                (self.mode[1],),
                {},
            ),
        )

    def enter(self, tx):
        self.mode = (
            torch.autograd.grad_mode._enter_inference_mode(self.target_values),
            tx.output.create_node(
                "call_function",
                torch.autograd.grad_mode._enter_inference_mode,
                (self.target_values,),
                {},
            ),
        )

    def module_name(self):
        return "torch.inference_mode"

    def fn_name(self):
        return "inference_mode"


class TorchFunctionDisableVariable(ContextWrappingVariable):
    """represents whether torch function overrides are enabled or not"""

    _guards_singleton = {Guard(GlobalStateSource(), GuardBuilder.TORCH_FUNCTION_STATE)}

    @staticmethod
    def create(tx, **kwargs):
        var = TorchFunctionDisableVariable(
            target_values=[False],
            initial_values=[torch._C._is_torch_function_enabled()],
            **kwargs,
        )
        # mlazos: I think this is here to make sure we don't reinvoke on clone()
        var._call_func(tx, [False])
        return var

    def __init__(self, target_values, initial_values=None, **kwargs):
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.guards = self.guards | self._guards_singleton

    def enter(self, tx):
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def _call_func(self, tx, values):
        assert len(values) == 1
        tx.output.set_torch_function_state(values[0])


class DeterministicAlgorithmsVariable(ContextWrappingVariable):
    """represents torch.{are_deterministic_algorithms_enabled,use_deterministic_algorithms}()"""

    _guards_singleton = {
        Guard(GlobalStateSource(), GuardBuilder.DETERMINISTIC_ALGORITHMS)
    }

    @staticmethod
    def create(tx, target_value, **kwargs):
        var = DeterministicAlgorithmsVariable(
            target_values=[target_value],
            initial_values=[torch.are_deterministic_algorithms_enabled()],
            **kwargs,
        )
        var._call_func(tx, [target_value])
        return var

    def __init__(self, target_values, initial_values=None, **kwargs):
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.guards = self.guards | self._guards_singleton

    def enter(self, tx):
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def _call_func(self, tx, values):
        assert len(values) == 1
        value = values[0]
        tx.output.create_node(
            "call_function", torch._C._set_deterministic_algorithms, (value,), {}
        ),
        torch._C._set_deterministic_algorithms(value)

    def module_name(self):
        return "torch"

    def fn_name(self):
        return "use_deterministic_algorithms"


class DisabledSavedTensorsHooksVariable(ContextWrappingVariable):
    """represents torch.autograd.graph.disable_saved_tensors_hook."""

    @staticmethod
    def create(tx, target_value, **kwargs):
        var = DisabledSavedTensorsHooksVariable(
            target_values=[target_value],
            initial_values=[
                torch._C._autograd._saved_tensors_hooks_get_disabled_error_message()
            ],
            **kwargs,
        )
        var._call_func(tx, [target_value])
        return var

    def __init__(self, target_values, initial_values=None, **kwargs):
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )

    def enter(self, tx):
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def _call_func(self, tx, values):
        assert len(values) == 1
        value = values[0]
        if value is not None:
            # Disable `saved_tensors_hooks` with message (`value`)
            # OR
            # we are exiting this context and restoring the previous message.
            tx.output.create_node(
                "call_function",
                torch._C._autograd._saved_tensors_hooks_disable,
                (value,),
                {},
            )
            torch._C._autograd._saved_tensors_hooks_disable(value)
        else:
            # We are exiting this context and if prev_message was None, we re-enable `saved_tensors_hooks`.
            tx.output.create_node(
                "call_function", torch._C._autograd._saved_tensors_hooks_enable, (), {}
            )
            torch._C._autograd._saved_tensors_hooks_enable()

    def module_name(self):
        return "torch.autograd.graph"

    def fn_name(self):
        return "disable_saved_tensors_hooks"


class AutocastModeVariable(ContextWrappingVariable):
    @staticmethod
    def create(func, args, kwargs):
        assert func in [
            torch.amp.autocast_mode.autocast,
            torch.cuda.amp.autocast,
            torch.cpu.amp.autocast,
        ]
        # device_type : str,
        # dtype : Optional[_dtype] = None,
        # enabled : bool = True,
        # cache_enabled : Optional[bool] = None):cache_enabled
        bound_args = inspect.signature(func).bind(*args, **kwargs)
        bound_args.apply_defaults()
        target_values = []
        kwargs.clear()

        for key in ["device_type", "dtype", "enabled", "cache_enabled"]:
            if key == "device_type" and func in [
                torch.cuda.amp.autocast,
                torch.cpu.amp.autocast,
            ]:
                arg = "cuda" if func is torch.cuda.amp.autocast else "cpu"
            else:
                arg = bound_args.arguments[key]
            if isinstance(arg, VariableTracker):
                target_values.append(arg.as_python_constant())
            else:
                target_values.append(arg)

        var = AutocastModeVariable(target_values, initial_values=None, **kwargs)
        return var

    def __init__(self, target_values, initial_values=None, **kwargs):
        mode = kwargs.pop("mode", None)
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.target_values = target_values
        self.mode = mode

    def exit(self, tx, *args):
        self.mode = (
            torch.amp._exit_autocast(self.mode[0]),
            tx.output.create_node(
                "call_function", torch.amp._exit_autocast, (self.mode[1],), {}
            ),
        )

    def enter(self, tx):
        self.mode = (
            torch.amp._enter_autocast(*self.target_values),
            tx.output.create_node(
                "call_function", torch.amp._enter_autocast, (*self.target_values,), {}
            ),
        )

    def module_name(self):
        return "torch.amp.autocast_mode"

    def fn_name(self):
        return "autocast"


class NullContextVariable(ContextWrappingVariable):
    """
    This class represents Python contextlib.nullcontext.
    It's used as a placeholder for other context managers that Dynamo doesn't
    support yet, e.g, torch.autograd.profiler.record_function.
    """

    def __init__(self, target_values=None, **kwargs):
        super().__init__(target_values=target_values, **kwargs)

    def enter(self, tx):
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def exit(self, tx, *args):
        return variables.ConstantVariable.create(
            None, **VariableTracker.propagate(self)
        )

    def module_name(self):
        return "contextlib"

    def fn_name(self):
        return "nullcontext"


class StreamContextVariable(ContextWrappingVariable):
    @staticmethod
    def create(tx, target_value, **kwargs):
        from .builder import wrap_fx_proxy_cls

        current_stream_method = get_interface_for_device(
            target_value.device
        ).current_stream
        current_stream = wrap_fx_proxy_cls(
            StreamVariable,
            tx,
            tx.output.create_proxy(
                "call_function",
                current_stream_method,
                (None,),
                {},
            ),
        )
        return StreamContextVariable(
            target_values=[target_value],
            initial_values=[current_stream],
            device=target_value.device,
            **kwargs,
        )

    def __init__(self, target_values, device, initial_values=None, **kwargs):
        super().__init__(
            target_values=target_values, initial_values=initial_values, **kwargs
        )
        self.device = device
        self.set_stream = get_interface_for_device(self.device).set_stream
        self.set_stream_id = get_interface_for_device(self.device)._set_stream_by_id

    def enter(self, tx):
        # stream generated inside of traced function
        if self.target_values[0].as_proxy() is not None:
            tx.output.create_proxy(
                "call_function",
                self.set_stream,
                (self.target_values[0].as_proxy(),),
                {},
            )
        # stream passed from outside of traced function
        else:
            stream = self.target_values[0].value
            tx.output.create_proxy(
                "call_function",
                self.set_stream_id,
                (stream.stream_id, stream.device_index, stream.device_type),
                {},
            )
        self.set_stream(self.target_values[0].value)

    def exit(self, tx, *args):
        tx.output.create_proxy(
            "call_function",
            self.set_stream,
            (self.initial_values[0].as_proxy(),),
            {},
        )
        self.set_stream(self.initial_values[0].value)

    def module_name(self):
        return "torch." + str(self.device)

    def fn_name(self):
        return "stream"


class StreamVariable(VariableTracker):
    def __init__(self, proxy, value, device, **kwargs):
        if proxy is not None and "example_value" in proxy.node.meta:
            assert proxy.node.meta["example_value"] == value
        assert (
            value.device.type == device
        ), "stream value is not equal to the passed device"
        super().__init__(**kwargs)
        self.proxy = proxy
        self.value = value
        self.device = device

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        assert hasattr(self.value, name), f"no stream method found named {name}"
        assert name in [
            "wait_stream",
            "synchronize",
            "query",
            "record_event",
            "wait_event",
        ], f" unsupported stream method {name}"

        from ..utils import proxy_args_kwargs
        from .builder import wrap_fx_proxy_cls

        if name in ("wait_stream", "synchronize", "wait_event"):
            tx.output.create_proxy(
                "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
            )
            return variables.ConstantVariable(None)
        elif name == "query":
            options = VariableTracker.propagate(self, args, kwargs.values())
            return wrap_fx_proxy_cls(
                target_cls=variables.ConstantVariable,
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
                ),
                **options,
            )
        elif name == "record_event":
            options = VariableTracker.propagate(self, args, kwargs.values())
            return wrap_fx_proxy_cls(
                target_cls=EventVariable,
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
                ),
                **options,
            )
        else:
            unimplemented(self.device + " stream method " + name + " unsupported")

    def as_proxy(self):
        return self.proxy


class EventVariable(VariableTracker):
    def __init__(self, proxy, value, **kwargs):
        if proxy is not None and "example_value" in proxy.node.meta:
            assert proxy.node.meta["example_value"] == value
        super().__init__(**kwargs)
        self.proxy = proxy
        self.value = value

    def call_method(
        self,
        tx,
        name,
        args: "List[VariableTracker]",
        kwargs: "Dict[str, VariableTracker]",
    ) -> "VariableTracker":
        from ..utils import proxy_args_kwargs
        from .builder import wrap_fx_proxy_cls

        if name in ("wait", "record", "synchronize"):
            tx.output.create_proxy(
                "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
            )
            return variables.ConstantVariable(None)
        elif name == "query":
            options = VariableTracker.propagate(self, args, kwargs.values())
            return wrap_fx_proxy_cls(
                target_cls=variables.ConstantVariable,
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_method", name, *proxy_args_kwargs([self] + args, kwargs)
                ),
                **options,
            )
        else:
            unimplemented(f"event method {name} unsupported")

    def as_proxy(self):
        return self.proxy


class WithExitFunctionVariable(VariableTracker):
    def __init__(self, ctx: ContextWrappingVariable, target, **kwargs):
        super().__init__(**kwargs)
        assert isinstance(ctx, ContextWrappingVariable)
        self.ctx = ctx
        self.target = target

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        assert not kwargs
        return self.ctx.exit(tx, *args)

    def reconstruct(self, codegen):
        # Note here we reconstruct the context manager rather than the
        # exit function.  The handler generated by BlockStackEntry
        # will re-enter the context in the resume function.
        output = AttrSource(
            codegen.tx.import_source(self.ctx.module_name()), self.ctx.fn_name()
        ).reconstruct(codegen)

        if codegen.tx.output.partial_convert:
            loads = [codegen.create_load_const(val) for val in self.ctx.target_values]
            output.extend(loads)
            output.extend(
                [
                    *create_call_function(len(loads), True),
                    create_instruction("SETUP_WITH", target=self.target),
                    create_instruction("POP_TOP"),
                ]
            )
        return output
