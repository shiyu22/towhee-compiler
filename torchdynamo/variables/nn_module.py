import functools
import inspect
import itertools
import re
import types
from typing import Dict
from typing import Sequence

import torch.nn
from typeguard import typechecked

from .. import skipfiles
from .. import variables as vars
from ..allowed_functions import is_allowed
from ..exc import RestartAnalysis
from ..exc import unimplemented
from ..guards import GuardBuilder
from ..mutation_guard import GenerationTracker
from ..source import AttrSource
from ..source import GetItemSource
from ..source import NNModuleSource
from ..source import NotNNModuleSource
from ..utils import is_lazy_module
from ..utils import istype
from ..utils import proxy_args_kwargs
from ..variables import Variable
from .base import MutableLocal
from .base import typestr
from .lists import SliceVariable
from .user_defined import UserDefinedObjectVariable


class NNModuleVariable(Variable):
    _nonvar_fields = ["module_type", "module_key"]

    def __init__(self, module_type: type, module_key: str, **kwargs):
        super(NNModuleVariable, self).__init__(**kwargs)
        self.module_type = module_type
        self.module_key = module_key
        assert self.source

    def python_type(self):
        return self.module_type

    def unpack_var_sequence(self, tx):
        # implement list/iter/tuple/etc calls
        base = tx.output.get_submodule(self.module_key)
        options = vars.propagate([self])
        assert isinstance(
            base, (torch.nn.ModuleList, torch.nn.ParameterList, torch.nn.Sequential)
        ), typestr(base)
        assert self.source
        return [
            tx.output.add_submodule(
                submod,
                self.module_key,
                idx,
                source=NNModuleSource(GetItemSource(self.source, idx)),
                **options,
            )
            for idx, submod in enumerate(base)
        ]

    def call_hasattr(self, tx, name: str) -> Variable:
        options = vars.propagate(self)
        mod = tx.output.get_submodule(self.module_key)
        result = hasattr(mod, name)
        return vars.constant(result, **options).add_guard(
            NNModuleSource(AttrSource(self.source, name)).create_guard(
                GuardBuilder.HASATTR
            )
        )

    def is_training(self, tx):
        mod = tx.output.get_submodule(self.module_key)
        return getattr(mod, "training", False)

    def convert_to_unspecialized(self, tx):
        """Restart analysis treating this module as an UnspecializedNNModuleVariable"""
        mod = tx.output.get_submodule(self.module_key)
        GenerationTracker.tag(mod)
        # GenerationTracker.mark_class_dynamic(type(mod))
        raise RestartAnalysis()

    def var_getattr(self, tx, name):
        options = vars.propagate(self)
        guards = options.get("guards", set())

        if self.source:
            source = AttrSource(self.source, name)
            options["source"] = source
        else:
            source = None

        base = tx.output.get_submodule(self.module_key)
        base_dict = object.__getattribute__(base, "__dict__")
        object_member = True
        all_class_attribute_names = set()
        for x in inspect.getmro(base.__class__):
            all_class_attribute_names.update(x.__dict__.keys())

        if not self.source:
            unimplemented("GETATTR with no source")

        if name in base_dict:
            subobj = base_dict[name]
        elif name in base_dict["_modules"] and name not in all_class_attribute_names:
            subobj = base_dict["_modules"][name]
        elif name in base_dict["_parameters"]:
            subobj = base_dict["_parameters"][name]
        elif name in base_dict["_buffers"]:
            subobj = base_dict["_buffers"][name]
        else:
            subobj = inspect.getattr_static(base, name)
            object_member = False

        if name == "__class__" and not object_member:
            return vars.UserDefinedClassVariable(base.__class__, **options)

        if object_member:
            return vars.build(tx, NNModuleSource(source))(subobj)
        else:
            if istype(subobj, property):
                return vars.userfunc(subobj.fget, guards=guards).call_function(
                    tx, [(self)], {}
                )
            elif istype(subobj, classmethod):
                return vars.usermethod(
                    subobj.__func__,
                    vars.UserDefinedObjectVariable(type(base), guards=guards),
                    **options,
                )
            elif istype(subobj, staticmethod):
                return vars.userfunc(subobj.__get__(base), **options)
            elif istype(subobj, types.FunctionType):
                return vars.usermethod(subobj, self, **options)
            else:
                unimplemented(f"class property {typestr(base)} {typestr(subobj)}")

        return vars.GetAttrVariable(self, name, **options)

    @typechecked
    def call_function(
        self, tx, args: Sequence[Variable], kwargs: Dict[str, Variable]
    ) -> Variable:
        options = vars.propagate(self, args, kwargs.values())
        mod = tx.output.get_submodule(self.module_key)
        is_lazy = is_lazy_module(mod)
        if (
            isinstance(mod, torch.nn.Sequential)
            and mod.__class__.forward is torch.nn.Sequential.forward
        ):
            # unroll Sequential()
            assert not kwargs
            (arg,) = args
            for idx, submod in enumerate(mod):
                tx.call_function(
                    tx.output.add_submodule(
                        submod,
                        self.module_key,
                        idx,
                        source=NNModuleSource(GetItemSource(self.source, idx)),
                        **options,
                    ),
                    [arg],
                    {},
                )
                arg = tx.pop()
            return arg
        elif is_allowed(mod.__class__):
            # The module type will change after it is called
            if is_lazy:
                self.module_type = mod.cls_to_become

            return vars.TensorVariable.create(
                tx=tx,
                proxy=tx.output.create_proxy(
                    "call_module",
                    self.module_key,
                    *proxy_args_kwargs(args, kwargs),
                ),
                nnmodule=mod,
                **options,
            )
        else:
            # for lazy modules, run the pre-hooks which will update the type
            # TODO mlazos: we don't fully support all of the hooks that exist,
            # so restrict using __call__ only to lazy modules for now
            if is_lazy:
                fn = mod.__class__.__call__
            else:
                fn = mod.__class__.forward

            return tx.inline_user_function_return(
                vars.userfunc(fn, **options),
                [self] + args,
                kwargs,
            )

    @typechecked
    def call_method(
        self,
        tx,
        name: str,
        args: Sequence[Variable],
        kwargs: Dict[str, Variable],
    ) -> Variable:
        options = vars.propagate(self, args, kwargs.values())
        key = self.module_key
        module = tx.output.get_submodule(key)

        if name == "forward":
            return self.call_function(tx, args, kwargs)

        if name == "_check_input_dim" and skipfiles.is_torch_inline_allowed(
            inspect.getfile(module.__class__._check_input_dim)
        ):
            return vars.constant(True, **options)

        if not all(
            x.is_python_constant() for x in itertools.chain(args, kwargs.values())
        ):
            raise unimplemented(f"non-const NNModule method {name}")

        def get_kwargs(*names):
            fn = getattr(module, name)
            bound_args = inspect.signature(fn).bind(
                *([x.as_python_constant() for x in args]),
                **{k: v.as_python_constant() for k, v in kwargs.items()},
            )
            bound_args.apply_defaults()
            bound_args = bound_args.arguments
            return {k: bound_args[k] for k in names}

        def wrap_values(items, getsource=AttrSource):
            result = []
            for name, submod in items:
                # layer.0.foo => layer[0].foo
                name = re.sub(r"[.]([0-9]+)([.]|$)", r"[\1]\2", name)
                src = NNModuleSource(getsource(self.source, name))
                result.append(
                    tx.output.add_submodule(
                        submod,
                        key,
                        name,
                        source=src,
                        **options,
                    )
                )
            return vars.listiter(result, mutable_local=MutableLocal(), **options)

        def named_embed(name, obj):
            return vars.basetuple(
                [
                    vars.constant(name, **options),
                    tx.output.add_submodule(
                        obj,
                        key,
                        name,
                        source=NNModuleSource(GetItemSource(self.source, name)),
                        **options,
                    ),
                ]
            )

        if name == "children":
            assert not (args or kwargs)
            return wrap_values(module.named_children())
        elif name == "named_parameters":
            result = []
            for name, param in module.named_parameters(
                **get_kwargs("prefix", "recurse")
            ):
                result.append(named_embed(name, param))
            return vars.listiter(result, mutable_local=MutableLocal(), **options)
        elif name == "named_modules":
            result = []
            for name, submod in module.named_modules(
                **get_kwargs("memo", "prefix", "remove_duplicate")
            ):
                result.append(named_embed(name, submod))
            return vars.listiter(result, mutable_local=MutableLocal(), **options)
        elif name == "parameters":
            return wrap_values(module.named_parameters(**get_kwargs("recurse")))
        elif name == "values":
            assert not (args or kwargs)
            return wrap_values(module.items(), GetItemSource)
        elif name == "items":
            assert not (args or kwargs)
            result = []
            for name, submod in module.items():
                result.append(named_embed(name, submod))
            return vars.listiter(result, mutable_local=MutableLocal(), **options)
        elif name == "__len__":
            assert not (args or kwargs)
            return vars.constant(len(module), **options)
        elif (
            name == "__contains__"
            and isinstance(module, (torch.nn.ModuleDict, torch.nn.ParameterDict))
            and args
            and args[0].is_python_constant()
        ):
            return vars.constant(
                args[0].as_python_constant() in module._modules, **options
            )
        elif name == "__getitem__":
            assert not kwargs and len(args) == 1
            assert type(module).__getitem__ in (
                torch.nn.ModuleDict.__getitem__,
                torch.nn.ModuleList.__getitem__,
                torch.nn.ParameterList.__getitem__,
            ), typestr(module)
            assert self.source

            if isinstance(args[0], SliceVariable):
                # Build a TupleVariable of NNModules
                result = []

                # Turn the slice into the list of integers
                keys = list(range(len(module)))[args[0].as_python_constant()]
                for idx, submod in enumerate(module[args[0].as_python_constant()]):
                    key = keys[idx]
                    src = NNModuleSource(GetItemSource(self.source, key))
                    result.append(
                        tx.output.add_submodule(
                            submod,
                            key,
                            source=src,
                            **options,
                        )
                    )
                return vars.basetuple(result, **options)

            key = args[0].as_python_constant()
            submod = module[key]
            return tx.output.add_submodule(
                submod,
                key,
                args[0].as_python_constant(),
                source=NNModuleSource(GetItemSource(self.source, key)),
                **options,
            )
        elif name == "_get_abs_string_index":
            assert not kwargs and len(args) == 1
            assert type(module) is torch.nn.ModuleList
            assert self.source

            return vars.constant(
                module._get_abs_string_index(args[0].as_python_constant()), **options
            )
        else:
            return super().call_method(tx, name, args, kwargs)


class UnspecializedNNModuleVariable(UserDefinedObjectVariable):
    """
    The above class will specialize on the id() of a module and place
    parameters on the torch.fx.GraphModule.  Giving one graph per
    module instance.  This version treats nn.Modules() like other user
    defined objects and will pass parameters into the FX graph as inputs.
    Giving one graph per module class.
    """

    def __init__(self, value, **kwargs):
        super(UnspecializedNNModuleVariable, self).__init__(value=value, **kwargs)
        if self.source and self.source.is_nn_module():
            # force guard checks even when `not config.guard_nn_modules``
            self.source = NotNNModuleSource(self.source)

    @staticmethod
    @functools.lru_cache(None)
    def _nn_module_method_ids():
        return {
            id(x.__code__)
            for x in torch.nn.Module.__dict__.values()
            if hasattr(x, "__code__")
        }

    def unpack_var_sequence(self, tx):
        try:
            fn = inspect.getattr_static(self.value_type, "__iter__")
        except AttributeError:
            raise NotImplementedError()

        if fn in (
            torch.nn.ModuleList.__iter__,
            torch.nn.ParameterList.__iter__,
            torch.nn.Sequential.__iter__,
        ):
            assert self.source
            return [
                vars.build(tx, source=GetItemSource(self.source, idx))(item).trace(self)
                for idx, item in enumerate(self.value)
            ]

        return super().unpack_var_sequence(tx)

    @typechecked
    def call_function(
        self, tx, args: Sequence[Variable], kwargs: Dict[str, Variable]
    ) -> Variable:
        # TODO mlazos: only support __call__ for lazy modules
        # until we can support a larger swath of python
        if is_lazy_module(self.value):
            fn = self.value_type.__call__
        else:
            fn = self.value_type.forward

        return (
            vars.userfunc(fn)
            .trace(self, args, kwargs)
            .call_function(tx, [self] + list(args), kwargs)
        )

    @typechecked
    def call_method(
        self,
        tx,
        name: str,
        args: Sequence[Variable],
        kwargs: Dict[str, Variable],
    ) -> Variable:
        options = vars.propagate(self, args, kwargs.values())

        if name not in getattr(self.value, "__dict__", {}):
            try:
                method = inspect.getattr_static(type(self.value), name)
            except AttributeError:
                method = None

            if method is torch.nn.Module.parameters:
                assert not args or kwargs
                options["guards"].add(
                    self.source.create_guard(GuardBuilder.NN_MODULE_PARAM_NAMES)
                )
                items = []
                for name, value in self.value.named_parameters():
                    items.append(
                        vars.build(tx, AttrSource(self.source, name))(value).trace(
                            options
                        )
                    )
                return vars.listiter(items, mutable_local=MutableLocal(), **options)

            if id(method.__code__) in self._nn_module_method_ids():
                unimplemented(f"UnspecializedNNModuleVariable missing {name}")

        return super().call_method(tx, name, args, kwargs)
