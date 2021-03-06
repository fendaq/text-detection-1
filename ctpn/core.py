import json

import cv2
import numpy as np
import matplotlib.pyplot as plt

import tensorflow as tf
import keras.backend as K

from keras import Model
from keras.applications.vgg16 import VGG16
from keras.layers import Conv2D, Lambda, Bidirectional, GRU, Activation
from keras.optimizers import Adam
from keras.utils import multi_gpu_model

from ctpn.lib import TextProposalConnectorOriented
from ctpn.lib import utils


def _rpn_loss_regr(y_true, y_pred):
    """
    smooth L1 loss

    y_ture [1][HXWX10][3] (class,regr)
    y_pred [1][HXWX10][2] (reger)
    """

    sigma = 9.0

    cls = y_true[0, :, 0]
    regr = y_true[0, :, 1:3]
    regr_keep = tf.where(K.equal(cls, 1))[:, 0]
    regr_true = tf.gather(regr, regr_keep)
    regr_pred = tf.gather(y_pred[0], regr_keep)
    diff = tf.abs(regr_true - regr_pred)
    less_one = tf.cast(tf.less(diff, 1.0 / sigma), 'float32')
    loss = less_one * 0.5 * diff ** 2 * sigma + tf.abs(1 - less_one) * (diff - 0.5 / sigma)
    loss = K.sum(loss, axis=1)

    return K.switch(tf.size(loss) > 0, K.mean(loss), K.constant(0.0))


def _rpn_loss_cls(y_true, y_pred):
    """
    softmax loss

    y_true [1][1][HXWX10] class
    y_pred [1][HXWX10][2] class
    """
    y_true = y_true[0][0]
    cls_keep = tf.where(tf.not_equal(y_true, -1))[:, 0]
    cls_true = tf.gather(y_true, cls_keep)
    cls_pred = tf.gather(y_pred[0], cls_keep)
    cls_true = tf.cast(cls_true, 'int64')
    # loss = K.sparse_categorical_crossentropy(cls_true,cls_pred,from_logits=True)
    loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=cls_true, logits=cls_pred)
    return K.switch(tf.size(loss) > 0, K.clip(K.mean(loss), 0, 10), K.constant(0.0))


def _reshape(x):
    b = tf.shape(x)
    x = tf.reshape(x, [b[0] * b[1], b[2], b[3]])    # (N x H, W, C)
    return x


def _reshape2(x):
    x1, x2 = x
    b = tf.shape(x2)
    x = tf.reshape(x1, [b[0], b[1], b[2], 256])     # (N, H, W, 256)
    return x


def _reshape3(x):
    b = tf.shape(x)
    x = tf.reshape(x, [b[0], b[1] * b[2] * 10, 2])  # (N, H x W x 10, 2)
    return x


class CTPN:

    def __init__(self, lr=0.00001, image_channels=3, vgg_trainable=True, weight_path=None, num_gpu=1):
        self.image_channels = image_channels
        self.image_shape = (None, None, image_channels)
        self.vgg_trainable = vgg_trainable
        self.num_gpu = num_gpu
        self.lr = lr
        self.model, self.parallel_model, self.predict_model = self.__build_model()
        if weight_path is not None:
            self.model.load_weights(weight_path)

    def __build_model(self):
        base_model = VGG16(weights=None, include_top=False, input_shape=self.image_shape)

        if self.vgg_trainable:
            base_model.trainable = True
        else:
            base_model.trainable = False

        input = base_model.input
        sub_output = base_model.get_layer('block5_conv3').output

        x = Conv2D(512, (3, 3), strides=(1, 1), padding='same', activation='relu',
                   name='rpn_conv1')(sub_output)

        x1 = Lambda(_reshape, output_shape=(None, 512))(x)

        x2 = Bidirectional(GRU(128, return_sequences=True), name='blstm')(x1)

        x3 = Lambda(_reshape2, output_shape=(None, None, 256))([x2, x])
        x3 = Conv2D(512, (1, 1), padding='same', activation='relu', name='lstm_fc')(x3)

        cls = Conv2D(10 * 2, (1, 1), padding='same', activation='linear', name='rpn_class')(x3)
        regr = Conv2D(10 * 2, (1, 1), padding='same', activation='linear', name='rpn_regress')(x3)

        cls = Lambda(_reshape3, output_shape=(None, 2), name='rpn_class_reshape')(cls)
        cls_prod = Activation('softmax', name='rpn_cls_softmax')(cls)

        regr = Lambda(_reshape3, output_shape=(None, 2), name='rpn_regress_reshape')(regr)

        predict_model = Model(input, [cls, regr, cls_prod])

        train_model = Model(input, [cls, regr])

        parallel_model = train_model
        if self.num_gpu > 1:
            parallel_model = multi_gpu_model(train_model, gpus=self.num_gpu)

        adam = Adam(self.lr)
        parallel_model.compile(optimizer=adam,
                               loss={'rpn_class_reshape': _rpn_loss_cls, 'rpn_regress_reshape': _rpn_loss_regr},
                               loss_weights={'rpn_class_reshape': 1.0, 'rpn_regress_reshape': 1.0},
                               metrics=['accuracy'])

        return train_model, parallel_model, predict_model

    def train(self, train_data_generator, epochs, **kwargs):
        self.parallel_model.fit_generator(train_data_generator, epochs=epochs, **kwargs)

    def predict(self, image_path, output_path=None, mode=1):
        img = cv2.imread(image_path)
        h, w, c = img.shape
        # zero-center by mean pixel
        m_img = img - utils.IMAGE_MEAN
        m_img = np.expand_dims(m_img, axis=0)

        cls, regr, cls_prod = self.predict_model.predict(m_img)
        anchor = utils.gen_anchor((int(h / 16), int(w / 16)), 16)

        bbox = utils.bbox_transfor_inv(anchor, regr)
        bbox = utils.clip_box(bbox, [h, w])

        # score > 0.7
        fg = np.where(cls_prod[0, :, 1] > 0.7)[0]
        select_anchor = bbox[fg, :]
        select_score = cls_prod[0, fg, 1]
        select_anchor = select_anchor.astype('int32')

        # filter size
        keep_index = utils.filter_bbox(select_anchor, 16)

        # nsm
        select_anchor = select_anchor[keep_index]
        select_score = select_score[keep_index]
        select_score = np.reshape(select_score, (select_score.shape[0], 1))
        nmsbox = np.hstack((select_anchor, select_score))
        keep = utils.nms(nmsbox, 0.3)
        select_anchor = select_anchor[keep]
        select_score = select_score[keep]

        # text line
        textConn = TextProposalConnectorOriented()
        text = textConn.get_text_lines(select_anchor, select_score, [h, w])

        text = text.astype('int32')

        if mode == 1:
            for i in text:
                cv2.line(img, (i[0], i[1]), (i[2], i[3]), (255, 0, 0), 2)
                cv2.line(img, (i[0], i[1]), (i[4], i[5]), (255, 0, 0), 2)
                cv2.line(img, (i[6], i[7]), (i[2], i[3]), (255, 0, 0), 2)
                cv2.line(img, (i[4], i[5]), (i[6], i[7]), (255, 0, 0), 2)

            plt.imshow(img)
            plt.show()
            if output_path is not None:
                cv2.imwrite(output_path, img)
        elif mode == 2:
            return text, img

    def config(self):
        return {
            "image_channels": self.image_channels,
            "vgg_trainable": self.vgg_trainable,
            "lr": self.lr
        }

    @staticmethod
    def save_config(obj, config_path):
        with open(config_path, "w+") as outfile:
            json.dump(obj.config(), outfile)

    @staticmethod
    def load_config(config_path):
        with open(config_path, "r") as infile:
            return dict(json.load(infile))
