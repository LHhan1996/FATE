#
#  Copyright 2021 The FATE Authors. All Rights Reserved.
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

from federatedml.secureprotol.hash.hash_factory import Hash
from federatedml.secureprotol.symmetric_encryption.cryptor_executor import CryptoExecutor
from federatedml.secureprotol.symmetric_encryption.pohlig_hellman_encryption import PohligHellmanCipherKey
from federatedml.statistic.intersect.base_intersect import Intersect
from federatedml.transfer_variable.transfer_class.ph_intersect_transfer_variable import PhIntersectTransferVariable
from federatedml.util import LOGGER


class PhIntersect(Intersect):
    """
    adapted from Secure Information Retrieval Module
    """
    def __init__(self):
        super().__init__()
        self.role = None
        self.transfer_variable = PhIntersectTransferVariable()
        self.commutative_cipher = None

    def load_params(self, param):
        self.only_output_key = param.only_output_key
        self.sync_intersect_ids = param.sync_intersect_ids
        self.ph_params = param.ph_params
        self.hash_operator = Hash(param.ph_params.hash_method)
        self.salt = self.ph_params.salt
        self.key_length = self.ph_params.key_length

    """    
    @staticmethod
    def record_original_id(k, v):
        if isinstance(k, str):
            restored_id = conversion.int_to_str(conversion.str_to_int(k))
        else:
            restored_id = k
        return (restored_id, k)
    """

    @staticmethod
    def _encrypt_id(data_instance, cipher, reserve_original_key=False, hash_operator=None, salt='',
                    reserve_original_value=False):
        """
        Encrypt the key (ID) of input Table
        :param cipher: cipher object
        :param data_instance: Table
        :param reserve_original_key: (enc_key, ori_key) if reserve_original_key == True, otherwise (enc_key, -1)
        :param hash_operator: if provided, use map_hash_encrypt
        :param salt: if provided, use for map_hash_encrypt
        : param reserve_original_value:
            (enc_key, (ori_key, val)) for reserve_original_key == True and reserve_original_value==True;
            (ori_key, (enc_key, val)) for only reserve_original_value == True.
        :return:
        """
        if reserve_original_key and reserve_original_value:
            if hash_operator:
                return cipher.map_hash_encrypt(data_instance, mode=5, hash_operator=hash_operator, salt=salt)
            return cipher.map_encrypt(data_instance, mode=5)
        if reserve_original_key:
            if hash_operator:
                return cipher.map_hash_encrypt(data_instance, mode=4, hash_operator=hash_operator, salt=salt)
            return cipher.map_encrypt(data_instance, mode=4)
        if reserve_original_value:
            if hash_operator:
                return cipher.map_hash_encrypt(data_instance, mode=3, hash_operator=hash_operator, salt=salt)
            return cipher.map_encrypt(data_instance, mode=3)
        if hash_operator:
            return cipher.map_hash_encrypt(data_instance, mode=1, hash_operator=hash_operator, salt=salt)
        return cipher.map_encrypt(data_instance, mode=1)

    @staticmethod
    def _decrypt_id(data_instance, cipher, reserve_value=False):
        """
        Decrypt the key (ID) of input Table
        :param data_instance: Table
        :param reserve_value: (e, De) if reserve_value, otherwise (De, -1)
        :return:
        """
        if reserve_value:
            return cipher.map_decrypt(data_instance, mode=0)
        else:
            return cipher.map_decrypt(data_instance, mode=1)

    """    
    @staticmethod
    def _find_intersection(id_local, id_remote):
        '''
        Find the intersection set of ENC_id
        :param id_local: Table in the form (EEg, -1)
        :param id_remote: Table in the form (EEh, -1)
        :return: Table in the form (EEi, -1)
        '''
        return id_local.join(id_remote, lambda v, u: -1)
    """

    def _generate_commutative_cipher(self):
        self.commutative_cipher = [
            CryptoExecutor(PohligHellmanCipherKey.generate_key(self.key_length)) for _ in self.host_party_id_list
        ]

    def _sync_commutative_cipher_public_knowledge(self):
        """
        guest -> host public knowledge
        :return:
        """
        pass

    def _exchange_id_list(self, id_list):
        """
        :param id_list: Table in the form (id, 0)
        :return:
        """
        pass

    def _sync_doubly_encrypted_id_list(self, id_list):
        """
        host -> guest
        :param id_list:
        :return:
        """
        pass

    def get_intersect_doubly_encrypted_id(self, data_instances):
        raise NotImplementedError("This method should not be called here")

    def decrypt_intersect_doubly_encrypted_id(self, id_list_intersect_cipher_cipher):
        raise NotImplementedError("This method should not be called here")

    def run_intersect(self, data_instances):
        LOGGER.info("Start PH Intersection")
        id_list_intersect_cipher_cipher = self.get_intersect_doubly_encrypted_id(data_instances)
        intersect_ids = self.decrypt_intersect_doubly_encrypted_id(id_list_intersect_cipher_cipher)
        return intersect_ids