#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import operator
from collections import Iterable

import numpy as np

from fate_arch.common import Party
from fate_arch.session import is_table
from federatedml.secureprotol.spdz.beaver_triples import beaver_triplets
from federatedml.secureprotol.spdz.tensor import fixedpoint_numpy
from federatedml.secureprotol.spdz.tensor.base import TensorBase
from federatedml.secureprotol.spdz.utils import NamingService
from federatedml.secureprotol.spdz.utils.random_utils import urand_tensor
from federatedml.secureprotol.fixedpoint import FixedPointNumber
from federatedml.util import LOGGER
from federatedml.util import fate_operator


def _table_binary_op(x, y, q_field, op):
    # return x.join(y, lambda a, b: op(a, b) % q_field)
    return x.join(y, lambda a, b: op(a, b))


def _table_scalar_op(x, d, op):
    return x.mapValues(lambda a: op(a, d))


def _table_scalar_mod_op(x, d, q_field, op):
    return x.mapValues(lambda a: op(a, d) % q_field)


def _table_dot_mod_func(it, q_field):
    ret = None
    for _, (x, y) in it:
        if ret is None:
            ret = np.tensordot(x, y, [[], []]) % q_field
        else:
            ret = (ret + np.tensordot(x, y, [[], []])) % q_field
    return ret


def _table_dot_func(it):
    ret = None
    for _, (x, y) in it:
        if ret is None:
            ret = np.tensordot(x, y, [[], []])
        else:
            ret += np.tensordot(x, y, [[], []])
    return ret


def table_dot(a_table, b_table):
    return a_table.join(b_table, lambda x, y: [x, y]) \
        .applyPartitions(lambda it: _table_dot_func(it)) \
        .reduce(lambda x, y: x + y)


def table_dot_mod(a_table, b_table, q_field):
    return a_table.join(b_table, lambda x, y: [x, y]) \
        .applyPartitions(lambda it: _table_dot_mod_func(it, q_field)) \
        .reduce(lambda x, y: x if y is None else y if x is None else x + y)


class FixedPointTensor(TensorBase):
    """
    a table based tensor
    """
    __array_ufunc__ = None

    def __init__(self, value, q_field, endec, tensor_name: str = None):
        super().__init__(q_field, tensor_name)
        self.value = value
        self.endec = endec
        self.tensor_name = NamingService.get_instance().next() if tensor_name is None else tensor_name

    def dot(self, other: 'FixedPointTensor', target_name=None):
        spdz = self.get_spdz()
        if target_name is None:
            target_name = NamingService.get_instance().next()

        a, b, c = beaver_triplets(a_tensor=self.value, b_tensor=other.value, dot=table_dot,
                                  q_field=self.q_field, he_key_pair=(spdz.public_key, spdz.private_key),
                                  communicator=spdz.communicator, name=target_name)

        x_add_a = (self + a).rescontruct(f"{target_name}_confuse_x")
        y_add_b = (other + b).rescontruct(f"{target_name}_confuse_y")
        cross = c - table_dot_mod(a, y_add_b, self.q_field) - table_dot_mod(x_add_a, b, self.q_field)
        if spdz.party_idx == 0:
            cross += table_dot_mod(x_add_a, y_add_b, self.q_field)
        cross = cross % self.q_field
        cross = self.endec.truncate(cross, self.get_spdz().party_idx)
        share = fixedpoint_numpy.FixedPointTensor(cross, self.q_field, self.endec, target_name)
        return share

    def dot_local(self, other: 'FixedPointTensor', target_name=None):
        if target_name is None:
            target_name = NamingService.get_instance().next()
        res = table_dot(self.value, other.value)
        return fixedpoint_numpy.FixedPointTensor(res, self.q_field, self.endec, target_name)

    def dot_array(self, array, fit_intercept=False):
        def _dot(x):
            if fit_intercept:
                coef = array[:-1]
                bias = array[-1]
                res = fate_operator.vec_dot(x, coef) + bias
            else:
                res = fate_operator.vec_dot(x, array)

            if not isinstance(res, np.ndarray):
                res = np.array([res])
            return res

        return self._boxed(self.value.mapValues(_dot))

    def convert_to_array_tensor(self):
        array = np.array([x[1] for x in self.value.collect()])
        return fixedpoint_numpy.FixedPointTensor(array, q_field=self.q_field, endec=self.endec)

    @classmethod
    def from_value(cls, value, **kwargs):
        spdz = cls.get_spdz()
        q_field = kwargs['q_field'] if 'q_field' in kwargs else spdz.q_field
        if 'encoder' in kwargs:
            encoder = kwargs['encoder']
        else:
            base = kwargs['base'] if 'base' in kwargs else 10
            frac = kwargs['frac'] if 'frac' in kwargs else 4
            encoder = fixedpoint_numpy.FixedPointEndec(q_field, base, frac)
        tensor_name = kwargs.get("tensor_name")
        # return FixedPointTensor(value, q_field, encoder, tensor_name)
        return cls(value, q_field, encoder, tensor_name)

    @classmethod
    def from_source(cls, tensor_name, source, **kwargs):
        spdz = cls.get_spdz()
        if 'encoder' in kwargs:
            encoder = kwargs['encoder']
        else:
            base = kwargs['base'] if 'base' in kwargs else 10
            frac = kwargs['frac'] if 'frac' in kwargs else 4
            q_field = kwargs['q_field'] if 'q_field' in kwargs else spdz.q_field
            encoder = fixedpoint_numpy.FixedPointEndec(q_field, base, frac)
        if is_table(source):
            source = encoder.encode(source)
            _pre = urand_tensor(spdz.q_field, source, use_mix=spdz.use_mix_rand)
            spdz.communicator.remote_share(share=_pre, tensor_name=tensor_name, party=spdz.other_parties[0])
            for _party in spdz.other_parties[1:]:
                r = urand_tensor(spdz.q_field, source, use_mix=spdz.use_mix_rand)
                spdz.communicator.remote_share(share=_table_binary_op(r, _pre, spdz.q_field, operator.sub),
                                               tensor_name=tensor_name, party=_party)
                _pre = r
            share = _table_binary_op(source, _pre, spdz.q_field, operator.sub)
        elif isinstance(source, np.ndarray):
            source = encoder.encode(source)
            _pre = urand_tensor(spdz.q_field, source, use_mix=spdz.use_mix_rand)
            spdz.communicator.remote_share(share=_pre, tensor_name=tensor_name, party=spdz.other_parties[0])
            for _party in spdz.other_parties[1:]:
                r = urand_tensor(spdz.q_field, source, use_mix=spdz.use_mix_rand)
                spdz.communicator.remote_share(share=(r - _pre) % spdz.q_field,
                                               tensor_name=tensor_name, party=_party)
                _pre = r
            share = (source - _pre) % spdz.q_field
        elif isinstance(source, Party):
            share = spdz.communicator.get_share(tensor_name=tensor_name, party=source)[0]
        else:
            raise ValueError(f"type={type(source)}")
        return FixedPointTensor(share, spdz.q_field, encoder, tensor_name)

    def get(self, tensor_name=None):
        LOGGER.debug(f"start get")
        return self.rescontruct(tensor_name)

    def rescontruct(self, tensor_name=None):
        from federatedml.secureprotol.spdz import SPDZ
        spdz = SPDZ.get_instance()
        share_val = self.value
        name = tensor_name or self.tensor_name

        if name is None:
            raise ValueError("name not specified")

        # remote share to other parties
        spdz.communicator.broadcast_rescontruct_share(share_val, name)

        # get shares from other parties
        for other_share in spdz.communicator.get_rescontruct_shares(name):
            share_val = _table_binary_op(share_val, other_share, self.q_field, operator.add)
        return share_val

    def __str__(self):
        return f"{self.tensor_name}: {self.value}"

    def __repr__(self):
        return self.__str__()

    def as_name(self, tensor_name):
        return self._boxed(value=self.value, tensor_name=tensor_name)

    @staticmethod
    def is_encrypted_number(table):
        value = table.first()[1]
        while isinstance(value, Iterable):
            value = value[0]
        if type(value).__name__ == "PaillierEncryptedNumber":
            return True
        return False

    def _basic_op(self, other, op):

        if isinstance(other, (int, np.int, float, np.float, FixedPointNumber)):
            z_value = _table_scalar_op(self.value, other, op)
            return self._boxed(z_value)

        if isinstance(other, FixedPointTensor):
            other = other.value

        if self.is_encrypted_number(other):
            z_value = self.value.join(other, op)
        else:
            z_value = _table_binary_op(self.value, other, self.q_field, op)

        return self._boxed(z_value)

    def __add__(self, other):
        return self._basic_op(other, operator.add)

    def __sub__(self, other):
        return self._basic_op(other, operator.sub)

    def __mul__(self, other):
        return self._basic_op(other, operator.mul)

    def __mod__(self, other):
        if not isinstance(other, (int, np.integer)):
            raise NotImplementedError("__mod__ support integer only")
        return self._boxed(_table_scalar_op(self.value, other, operator.mod))

    def _boxed(self, value, tensor_name=None):
        return FixedPointTensor(value=value, q_field=self.q_field, endec=self.endec, tensor_name=tensor_name)


class PaillierFixedPointTensor(FixedPointTensor):

    def dot_array(self, array):
        def _dot(x):
            res = fate_operator.vec_dot(x, array)
            if not isinstance(res, np.ndarray):
                res = np.array([res])
            return res

        return self._boxed(self.value.mapValues(_dot))

    def dot_local(self, other: 'FixedPointTensor', target_name=None):
        if target_name is None:
            target_name = NamingService.get_instance().next()
        res = table_dot(self.value, other.value)
        return fixedpoint_numpy.PaillierFixedPointTensor(res, self.q_field, self.endec, target_name)

    def _basic_op(self, other, op):
        if isinstance(other, (float, np.float)):
            other = self.endec.encode(other)
            z_value = _table_scalar_op(self.value, other, op)
            return self._boxed(z_value)
        elif isinstance(other, (int, np.int, FixedPointNumber)):
            z_value = _table_scalar_op(self.value, other, op)
            return self._boxed(z_value)

        if isinstance(other, FixedPointTensor):
            other = other.value
        z_value = self.value.join(other, op)
        return self._boxed(z_value)

    def _boxed(self, value, tensor_name=None):
        return PaillierFixedPointTensor(value=value, q_field=self.q_field, endec=self.endec, tensor_name=tensor_name)
