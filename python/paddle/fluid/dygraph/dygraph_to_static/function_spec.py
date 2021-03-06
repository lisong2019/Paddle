# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import six
import inspect
import numpy as np
import collections
import paddle
from paddle.fluid import core
from paddle.fluid.dygraph import layers
from paddle.fluid.layers.utils import flatten
from paddle.fluid.layers.utils import pack_sequence_as
from paddle.fluid.dygraph.base import switch_to_static_graph
from paddle.fluid.dygraph.dygraph_to_static.utils import parse_arg_and_kwargs
from paddle.fluid.dygraph.dygraph_to_static.utils import type_name
from paddle.fluid.dygraph.dygraph_to_static.utils import func_to_source_code


class FunctionSpec(object):
    """
    Wrapper class for a function for class method.
    """

    def __init__(self, function, input_spec=None):
        self._dygraph_function = function
        if input_spec is None:
            self._input_spec = None
            self._flat_input_spec = None
        else:
            self._input_spec = self._verify_input_spec(input_spec)
            self._flat_input_spec = flatten(self._input_spec)

        # parse full argument names list.
        self._arg_names, self._default_kwargs = parse_arg_and_kwargs(function)

    def unified_args_and_kwargs(self, args, kwargs):
        """
        Moves kwargs with default value into arguments list to keep `args` contain the same length
        value as function definition.
        
        For example: 
        
            Given function definition: `def foo(x, a=1, b=2)`, 
            when calling it by `foo(23)`, the args is `[23]`, kwargs is `{a=1, b=2}`.
            In this function, it will return args with `[23, 1, 2]`, kwargs with `{}`

        Args:
            args(tuple): tuple of input arguments value of decorated function.
            kwargs(dict): dict of input keyword arguments value of decorated function.

        Return:
            New arguments tuple containing default kwargs value.
        """
        if len(self._arg_names) < len(args):
            error_msg = "The decorated function `{}` requires {} arguments: {}, but received {} with {}.".format(
                self._dygraph_function.__name__,
                len(self._arg_names), self._arg_names, len(args), args)
            if args and inspect.isclass(args[0]):
                error_msg += "\n\tMaybe the function has more than one decorator, we don't support this for now."
                raise NotImplementedError(error_msg)
            else:
                raise ValueError(error_msg)

        args = list(args)

        for i in six.moves.range(len(args), len(self._arg_names)):
            arg_name = self._arg_names[i]
            if arg_name in kwargs:
                args.append(kwargs[arg_name])
                del kwargs[arg_name]
            else:
                if arg_name not in self._default_kwargs:
                    raise ValueError(
                        "`{}()` requires `{}` arguments, but not found in input `args`: {} and `kwargs`: {}.".
                        format(self._dygraph_function.__name__, arg_name, args,
                               kwargs))
                args.append(self._default_kwargs[arg_name])

        return tuple(args), kwargs

    def args_to_input_spec(self, args, kwargs):
        """
        Converts input arguments into InputSpec.
        
        1. If specific input_spec, use them to construct feed layers.
        2. If input_spec is None, consider all Tensor and Numpy.ndarray as feed layers

        Args:
            args(tuple): tuple of input arguments value of function containing default kwargs value.
            kwargs(dict): kwargs arguments received by **kwargs.

        Return:
            Same nest structure with args by replacing value with InputSpec.
        """
        input_with_spec = []

        if self._input_spec is not None:
            # Note: Because the value type and length of `kwargs` is uncertain.
            # So we don't support to deal this case while specificing `input_spec` currently.
            if kwargs:
                raise ValueError(
                    "{} got unexpected keyword arguments: {}. Cannot trace the function when `input_spec` is specificed.".
                    format(self._dygraph_function.__name__, kwargs))

            # Note: The length of `input_spec` can be greater than `args`,
            # because `args` may contains non-tensor value merged form `kwargs`
            # after `unified_args_and_kwargs`.
            if len(args) < len(self._input_spec):
                raise ValueError(
                    "Requires len(arguments) >= len(input_spec), but received len(args):{} < len(InputSpec): {}".
                    format(len(args), len(self._input_spec)))

            # replace argument with corresponding InputSpec.
            input_with_spec = convert_to_input_spec(args, self._input_spec)
        else:
            for idx, input_var in enumerate(flatten(args)):
                if isinstance(input_var, np.ndarray):
                    input_var = paddle.static.InputSpec.from_numpy(input_var)
                elif isinstance(input_var, core.VarBase):
                    input_var = paddle.static.InputSpec.from_tensor(input_var)

                input_with_spec.append(input_var)

            input_with_spec = pack_sequence_as(args, input_with_spec)

        return input_with_spec

    @switch_to_static_graph
    def to_static_inputs_with_spec(self, input_with_spec, main_program):
        """
        Constructs feed layer by inputs with InputSpec information for main program.

        Args:
            input_with_spec(tuple): input arguments by replacing argument with InputSpec.
            main_program(Program): main program for inserting feed layer.
        """
        flat_input_spec = flatten(input_with_spec)

        inputs = []
        block = main_program.global_block()
        for i, var_spec in enumerate(flat_input_spec):
            if isinstance(var_spec, paddle.static.InputSpec):
                feed_layer = block.create_var(
                    # TODO(Aurelius84): consider a more elegant way to name this
                    name=var_spec.name or "feed_%s" % i,
                    shape=var_spec.shape,
                    dtype=var_spec.dtype,
                    is_data=True,
                    need_check_feed=False)
            else:
                feed_layer = var_spec
            inputs.append(feed_layer)

        return pack_sequence_as(input_with_spec, inputs)

    def _verify_input_spec(self, input_spec):
        """
        Verifies the `input_spec` and its element type is valid.
        """
        if not isinstance(input_spec, (tuple, list)):
            raise TypeError(
                "The type(input_spec) should be one of (tuple, list), but received {}.".
                format(type_name(input_spec)))
        input_spec = tuple(input_spec)
        for spec in flatten(input_spec):
            if not isinstance(spec, paddle.static.InputSpec):
                raise ValueError(
                    "The type(elem) from input_spec should be `InputSpec`, but received {}.".
                    format(type_name(spec)))

        return input_spec

    def __repr__(self):
        return "function: {}({}), input_spec: {}".format(
            self._dygraph_function.__name__, ','.join(self._arg_names),
            self._input_spec)

    @property
    def dygraph_function(self):
        return self._dygraph_function

    @property
    def args_name(self):
        return self._arg_names

    @property
    def input_spec(self):
        return self._input_spec

    @property
    def flat_input_spec(self):
        return self._flat_input_spec

    @property
    def code(self):
        return func_to_source_code(self._dygraph_function)


def get_parameters(layer_instance, include_sublayer=True):
    """
    Returns parameters of decorated layers. If set `include_sublayer` True,
    the parameters created in sub layers will be added.
    """
    params = collections.OrderedDict()
    if layer_instance is not None:
        if isinstance(layer_instance, layers.Layer):
            if include_sublayer:
                params = layer_instance.parameters()
                names = [p.name for p in params]
                params = collections.OrderedDict(zip(names, params))
            else:
                params = layer_instance._parameters
        else:
            raise TypeError(
                "Type of `layer_instance` should be nn.Layer, but received {}".
                format(type_name(layer_instance)))

    return params


def get_buffers(layer_instance, include_sublayer=True):
    """
    Returns Variable buffers of decorated layers. If set `include_sublayer` True,
    the Variable buffers created in sub layers will be added.
    """
    buffers = collections.OrderedDict()
    if layer_instance is not None:
        if isinstance(layer_instance, layers.Layer):
            if include_sublayer:
                buffers = layer_instance.buffers()
                names = [buffer.name for buffer in buffers]
                buffers = collections.OrderedDict(zip(names, buffers))
            else:
                buffers = layer_instance._buffers
        else:
            raise TypeError(
                "Type of `layer_instance` should be nn.Layer, but received {}".
                format(type_name(layer_instance)))
    return buffers


def convert_to_input_spec(inputs, input_spec):
    """
    Replaces tensor in structured `inputs` by InputSpec in `input_spec`.
    
    Args:
        inputs(list|dict): nested structure list or dict.
        input_spec(list|dict): same nested structure list or dict as inputs. 

    
    Return:
        Same structure with inputs by replacing the element with specified InputSpec.
    """

    def check_type_and_len(input, spec, check_length=False):
        if type(input) is not type(spec):
            raise TypeError('type(input) should be {}, but received {}.'.format(
                type(spec), type(input)))
        if check_length and len(input) < len(spec):
            raise ValueError(
                'Requires len(inputs) >= len(input_spec), but received len(inputs):{} < len(input_spec):{}'.
                format(len(inputs), len(input_spec)))

    if isinstance(input_spec, (tuple, list)):
        input_with_spec = []
        check_type_and_len(inputs, input_spec, True)

        for i, spec in enumerate(input_spec):
            out_spec = convert_to_input_spec(inputs[i], spec)
            input_with_spec.append(out_spec)

        # Note: If the rest inputs contain tensor or numpy.ndarray
        # without specific InputSpec, raise warning.
        if len(inputs) > len(input_spec):
            for rest_input in inputs[len(input_spec):]:
                if isinstance(rest_input, (core.VarBase, np.ndarray)):
                    logging.warning(
                        "The inputs constain `{}` without specificing InputSpec, its shape and dtype will be treated immutable. "
                        "Please specific InputSpec information in `@declarative` if you expect them as mutable inputs.".
                        format(type_name(rest_input)))
        input_with_spec.extend(inputs[len(input_spec):])

        return input_with_spec
    elif isinstance(input_spec, dict):
        input_with_spec = {}
        check_type_and_len(inputs, input_spec, True)
        for name, input in six.iteritems(inputs):
            if name in input_spec:
                input_with_spec[name] = convert_to_input_spec(input,
                                                              input_spec[name])
            else:
                input_with_spec[name] = input
        return input_with_spec
    elif isinstance(input_spec, paddle.static.InputSpec):
        return input_spec
    else:
        raise TypeError(
            "The type(input_spec) should be a `InputSpec` or dict/list/tuple of it, but received {}.".
            type_name(input_spec))
