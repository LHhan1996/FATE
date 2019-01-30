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

import numpy as np
from federatedml.ftl.plain_ftl import PlainFTLGuestModel, PlainFTLHostModel
from federatedml.ftl.encryption.encryption import decrypt_array, decrypt_matrix, decrypt_scalar
from federatedml.ftl.eggroll_computation.helper import compute_sum_XY, \
    compute_XY, encrypt_matrix, compute_XY_plus_Z, \
    encrypt_matmul_2_ob, encrypt_matmul_3, compute_X_plus_Y


class FasterEncryptedFTLGuestModel(PlainFTLGuestModel):

    def __init__(self, local_model, model_param, public_key=None, private_key=None, is_trace=False):
        super(FasterEncryptedFTLGuestModel, self).__init__(local_model, model_param, is_trace)
        self.public_key = public_key
        self.private_key = private_key

    def set_public_key(self, public_key):
        self.public_key = public_key

    def set_private_key(self, private_key):
        self.private_key = private_key

    def send_components(self):
        self._compute_components()
        # Important: send self.y_A_u_A_2 to host indirectly computing part of loss
        components = [self.y_overlap_phi, self.mapping_comp_A, self.phi, self.phi_2]
        return self.__encrypt_components(components)

    def __encrypt_components(self, components):
        encrypt_comp_0 = encrypt_matrix(self.public_key, components[0])
        encrypt_comp_1 = encrypt_matrix(self.public_key, components[1])
        encrypt_comp_2 = encrypt_matrix(self.public_key, components[2])
        encrypt_comp_3 = encrypt_matrix(self.public_key, components[3])
        return [encrypt_comp_0, encrypt_comp_1, encrypt_comp_2, encrypt_comp_3]

    def receive_components(self, components):
        self.U_B_overlap = components[0]
        self.mapping_comp_B = components[1]
        self.__precompute()

    def __precompute(self):
        U_B_overlap_ex = np.expand_dims(self.U_B_overlap, axis=1)
        uB_overlap_y_overlap_2_phi_2 = encrypt_matmul_3(U_B_overlap_ex, self.y_overlap_2_phi_2)
        self.precomputed_component = np.squeeze(uB_overlap_y_overlap_2_phi_2, axis=1)

    def send_precomputed_components(self):
        return [self.precomputed_component]

    def receive_precomputed_components(self, components):
        self.y_overlap_2_phi_uB_overlap_2 = components[0]
        self.phi_uB_overlap_2_phi = components[1]
        self._update_gradients()
        self._update_loss()

    def _update_gradients(self):

        # # y_overlap2 have shape (len(overlap_indexes), 1),
        # # y_A_u_A has shape (1, feature_dim),
        # # y_overlap2_y_A_u_A has shape (len(overlap_indexes), 1, feature_dim)
        # y_overlap2_y_A_u_A = np.expand_dims(self.y_overlap2 * self.y_A_u_A, axis=1)
        #
        # # U_B_2_overlap has shape (len(overlap_indexes), feature_dim, feature_dim)
        # # tmp has shape (len(overlap_indexes), feature_dim)
        # tmp1 = encrypt_matmul_3(y_overlap2_y_A_u_A, self.U_B_2_overlap)
        # tmp2 = 0.25 * np.squeeze(tmp1, axis=1)

        if self.is_trace:
            self.logger.debug("y_overlap_2_phi_uB_overlap_2 shape" + str(self.y_overlap_2_phi_uB_overlap_2.shape))

        y_overlap = np.tile(self.y_overlap, (1, self.U_B_overlap.shape[-1]))
        y_overlap_uB_overlap = compute_sum_XY(y_overlap * 0.5, self.U_B_overlap)

        encrypt_const = np.sum(self.y_overlap_2_phi_uB_overlap_2, axis=0) - y_overlap_uB_overlap
        encrypt_const_overlap = np.tile(encrypt_const, (len(self.overlap_indexes), 1))
        encrypt_const_nonoverlap = np.tile(encrypt_const, (len(self.non_overlap_indexes), 1))
        y_non_overlap = np.tile(self.y[self.non_overlap_indexes], (1, self.U_B_overlap.shape[-1]))

        if self.is_trace:
            self.logger.debug("encrypt_const shape:" + str(encrypt_const.shape))
            self.logger.debug("encrypt_const_overlap shape" + str(encrypt_const_overlap.shape))
            self.logger.debug("encrypt_const_nonoverlap shape" + str(encrypt_const_nonoverlap.shape))
            self.logger.debug("y_non_overlap shape" + str(y_non_overlap.shape))

        encrypt_grad_A_nonoverlap = compute_XY(self.alpha * y_non_overlap / len(self.y), encrypt_const_nonoverlap)
        encrypt_grad_A_overlap = compute_XY_plus_Z(self.alpha * y_overlap / len(self.y), encrypt_const_overlap, self.mapping_comp_B)

        if self.is_trace:
            self.logger.debug("encrypt_grad_A_nonoverlap shape" + str(encrypt_grad_A_nonoverlap.shape))
            self.logger.debug("encrypt_grad_A_overlap shape" + str(encrypt_grad_A_overlap.shape))

        encrypt_grad_loss_A = [[0 for _ in range(self.U_B_overlap.shape[1])] for _ in range(len(self.y))]
        # TODO: need more efficient way to do following task
        for i, j in enumerate(self.non_overlap_indexes):
            encrypt_grad_loss_A[j] = encrypt_grad_A_nonoverlap[i]
        for i, j in enumerate(self.overlap_indexes):
            encrypt_grad_loss_A[j] = encrypt_grad_A_overlap[i]

        encrypt_grad_loss_A = np.array(encrypt_grad_loss_A)

        if self.is_trace:
            self.logger.debug("encrypt_grad_loss_A shape" + str(encrypt_grad_loss_A.shape))
            self.logger.debug("encrypt_grad_loss_A" + str(encrypt_grad_loss_A))

        self.loss_grads = encrypt_grad_loss_A
        self.encrypt_grads_W, self.encrypt_grads_b = self.localModel.compute_encrypted_params_grads(
            self.X, encrypt_grad_loss_A)

    def send_gradients(self):
        return self.encrypt_grads_W, self.encrypt_grads_b

    def receive_gradients(self, gradients):
        self.localModel.apply_gradients(gradients)

    def send_loss(self):
        return self.loss

    def receive_loss(self, loss):
        self.loss = loss

    def _update_loss(self):
        U_A_overlap_prime = - self.U_A_overlap / self.feature_dim
        loss_overlap = np.sum(compute_sum_XY(U_A_overlap_prime, self.U_B_overlap))
        loss_Y = self.__compute_encrypt_loss_y(self.U_B_overlap, self.y_overlap, self.phi)
        self.loss = self.alpha * loss_Y + loss_overlap

    def __compute_encrypt_loss_y(self, encrypt_U_B_overlap, y_overlap, y_A_u_A):
        encrypt_UB_yAuA = encrypt_matmul_2_ob(encrypt_U_B_overlap, y_A_u_A.transpose())
        encrypt_loss_Y = (-0.5 * compute_sum_XY(y_overlap, encrypt_UB_yAuA)[0] + 1.0 / 8 * np.sum(self.phi_uB_overlap_2_phi)) + len(y_overlap) * np.log(2)
        return encrypt_loss_Y

    def get_loss_grads(self):
        return self.loss_grads


class FasterEncryptedFTLHostModel(PlainFTLHostModel):

    def __init__(self, local_model, model_param, public_key=None, private_key=None, is_trace=False):
        super(FasterEncryptedFTLHostModel, self).__init__(local_model, model_param, is_trace)
        self.public_key = public_key
        self.private_key = private_key

    def set_public_key(self, public_key):
        self.public_key = public_key

    def set_private_key(self, private_key):
        self.private_key = private_key

    def send_components(self):
        self._compute_components()
        components = [self.U_B_overlap, self.mapping_comp_B]
        return self.__encrypt_components(components)

    def __encrypt_components(self, components):
        encrypt_UB_1 = encrypt_matrix(self.public_key, components[0])
        encrypt_UB_2 = encrypt_matrix(self.public_key, components[1])
        return [encrypt_UB_1, encrypt_UB_2]

    def receive_components(self, components):
        self.comp_A_beta2 = components[0]
        self.mapping_comp_A = components[1]
        self.y_A_u_A = components[2]
        self.y_A_u_A_2 = components[3]
        self.__precompute()

    def __precompute(self):
        # ------------------------------------------------------------
        # y_overlap2 have shape (len(overlap_indexes), 1),
        # y_A_u_A has shape (1, feature_dim),
        # y_overlap_2_phi has shape (len(overlap_indexes), 1, feature_dim)
        # y_overlap_2_phi = np.expand_dims(self.y_overlap2 * self.y_A_u_A, axis=1)
        # U_B_2_overlap has shape (len(overlap_indexes), feature_dim, feature_dim)
        # tmp has shape (len(overlap_indexes), feature_dim)
        # y_overlap_2_phi_uB_overlap_2 = encrypt_matmul_3(y_overlap_2_phi, self.U_B_2_overlap)
        # tmp2 = 0.25 * np.squeeze(y_overlap_2_phi_uB_overlap_2, axis=1)

        y_overlap_2_phi = np.expand_dims(np.tile(self.y_A_u_A, (len(self.overlap_indexes), 1)), axis=1)
        print("y_overlap_2_phi.shape", y_overlap_2_phi.shape)
        y_overlap_2_phi_uB_overlap_2 = encrypt_matmul_3(y_overlap_2_phi, self.U_B_overlap_2)
        print("y_overlap_2_phi_uB_overlap_2 shape", str(y_overlap_2_phi_uB_overlap_2.shape))
        self.precomputed_grad_component = 0.25 * np.squeeze(y_overlap_2_phi_uB_overlap_2, axis=1)

        # compute part of the loss for guest
        phi_uB_overlap_2_phi = 0
        for UB_row in self.U_B_overlap:
            UB_row = UB_row.reshape(1, -1)
            phi_uB_overlap_2_phi += encrypt_matmul_2_ob(encrypt_matmul_2_ob(UB_row, self.y_A_u_A_2), UB_row.transpose())
        self.precomputed_loss_component = phi_uB_overlap_2_phi

    def send_precomputed_components(self):
        return [self.precomputed_grad_component, self.precomputed_loss_component]

    def receive_precomputed_components(self, components):
        self.encrypted_U_B_comp_A_beta1 = components[0]
        self._update_gradients()

    def _update_gradients(self):
        # U_B_overlap_ex = np.expand_dims(self.U_B_overlap, axis=1)
        # grads = self.localModel.compute_gradients(self.X[self.overlap_indexes])

        # # following computed from guest
        # encrypted_U_B_comp_A_beta1 = encrypt_matmul_3(U_B_overlap_ex, self.comp_A_beta1)
        # encrypted_U_B_comp_A_beta1 = np.squeeze(encrypted_U_B_comp_A_beta1, axis=1)

        encrypted_grad_l1_B = compute_X_plus_Y(self.encrypted_U_B_comp_A_beta1, self.comp_A_beta2)
        encrypted_grad_loss_B = compute_X_plus_Y(self.alpha * encrypted_grad_l1_B, self.mapping_comp_A)

        self.loss_grads = encrypted_grad_loss_B
        self.encrypt_grads_W, self.encrypt_grads_b = self.localModel.compute_encrypted_params_grads(
            self.X[self.overlap_indexes], encrypted_grad_loss_B)

    def send_gradients(self):
        return self.encrypt_grads_W, self.encrypt_grads_b

    def receive_gradients(self, gradients):
        self.localModel.apply_gradients(gradients)

    def get_loss_grads(self):
        return self.loss_grads


class LocalFasterEncryptedFederatedTransferLearning(object):

    def __init__(self, guest: FasterEncryptedFTLGuestModel, host: FasterEncryptedFTLHostModel, private_key=None):
        super(LocalFasterEncryptedFederatedTransferLearning, self).__init__()
        self.guest = guest
        self.host = host
        self.private_key = private_key

    def fit(self, X_A, X_B, y, overlap_indexes, non_overlap_indexes):
        self.guest.set_batch(X_A, y, non_overlap_indexes, overlap_indexes)
        self.host.set_batch(X_B, overlap_indexes)

        comp_B = self.host.send_components()
        comp_A = self.guest.send_components()

        self.guest.receive_components(comp_B)
        self.host.receive_components(comp_A)

        precomputed_components_B = self.host.send_precomputed_components()
        precomputed_components_A = self.guest.send_precomputed_components()

        self.guest.receive_precomputed_components(precomputed_components_B)
        self.host.receive_precomputed_components(precomputed_components_A)

        encrypt_gradients_A = self.guest.send_gradients()
        encrypt_gradients_B = self.host.send_gradients()

        self.guest.receive_gradients(self.__decrypt_gradients(encrypt_gradients_A))
        self.host.receive_gradients(self.__decrypt_gradients(encrypt_gradients_B))

        encrypt_loss = self.guest.send_loss()
        loss = self.__decrypt_loss(encrypt_loss)

        return loss

    def predict(self, X_B):
        msg = self.host.predict(X_B)
        return self.guest.predict(msg)

    def __decrypt_gradients(self, encrypt_gradients):
        return decrypt_matrix(self.private_key, encrypt_gradients[0]), decrypt_array(self.private_key, encrypt_gradients[1])

    def __decrypt_loss(self, encrypt_loss):
        return decrypt_scalar(self.private_key, encrypt_loss)
