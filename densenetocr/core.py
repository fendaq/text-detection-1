import json

import keras.backend as K
import numpy as np
from PIL import Image
from keras import Input, Model
from keras.layers import Conv2D, BatchNormalization, Activation, Dropout, AveragePooling2D, ZeroPadding2D, Permute, \
    TimeDistributed, Flatten, Dense, Lambda
from keras.layers.merge import concatenate
from keras.optimizers import Adam
from keras.regularizers import l2
from keras.utils.multi_gpu_utils import multi_gpu_model

from densenetocr.data_loader import DataLoader


def _dense_block(x, nb_layers, nb_filter, growth_rate, dropout_rate=0.2, weight_decay=1e-4):
    for i in range(nb_layers):
        cb = _conv_block(x, growth_rate, dropout_rate, weight_decay)
        x = concatenate([x, cb])
        nb_filter += growth_rate
    return x, nb_filter


def _conv_block(input, growth_rate, dropout_rate=None, weight_decay=1e-4):
    x = BatchNormalization(epsilon=1.1e-5)(input)
    x = Activation('relu')(x)
    x = Conv2D(growth_rate, (3, 3), kernel_initializer='he_normal', padding='same')(x)
    if dropout_rate:
        x = Dropout(dropout_rate)(x)
    return x


def _transition_block(input, nb_filter, dropout_rate=None, pooltype=1, weight_decay=1e-4):
    x = BatchNormalization(epsilon=1.1e-5)(input)
    x = Activation('relu')(x)
    x = Conv2D(nb_filter, (1, 1), kernel_initializer='he_normal', padding='same', use_bias=False,
               kernel_regularizer=l2(weight_decay))(x)

    if dropout_rate:
        x = Dropout(dropout_rate)(x)

    if pooltype == 2:
        x = AveragePooling2D((2, 2), strides=(2, 2))(x)
    elif pooltype == 1:
        x = ZeroPadding2D(padding=(0, 1))(x)
        x = AveragePooling2D((2, 2), strides=(2, 1))(x)
    elif pooltype == 3:
        x = AveragePooling2D((2, 2), strides=(2, 1))(x)

    return x, nb_filter


def _ctc_loss(args):
    labels, y_pred, input_length, label_length = args
    return K.ctc_batch_cost(labels, y_pred, input_length, label_length)


class DenseNetOCR:

    def __init__(self,
                 num_classes,
                 lr=0.0005,
                 image_height=32,
                 image_channels=1,
                 maxlen=50,
                 dropout_rate=0.2,
                 weight_decay=1e-4,
                 filters=64,
                 weight_path=None,
                 num_gpu=1):
        self.image_shape = (image_height, None, image_channels)
        self.lr = lr
        self.image_height, self.image_channels = image_height, image_channels
        self.maxlen = maxlen
        self.dropout_rate = dropout_rate
        self.weight_decay = weight_decay
        self.filters = filters
        self.num_classes = num_classes
        self.num_gpu = num_gpu
        self.base_model, self.model, self.parallel_model = self.__build_model()
        if weight_path is not None:
            self.base_model.load_weights(weight_path)

    def config(self):
        return {
            "lr": self.lr,
            "num_classes": self.num_classes,
            "image_height": self.image_height,
            "image_channels": self.image_channels,
            "maxlen": self.maxlen,
            "dropout_rate": self.dropout_rate,
            "weight_decay": self.weight_decay,
            "filters": self.filters
        }

    def __build_model(self):
        input = Input(shape=self.image_shape, name="the_input")
        nb_filter = self.filters

        x = Conv2D(nb_filter, (5, 5), strides=(2, 2), kernel_initializer='he_normal', padding='same',
                   use_bias=False, kernel_regularizer=l2(self.weight_decay))(input)

        # 64 +  8 * 8 = 128
        x, nb_filter = _dense_block(x, 8, nb_filter, 8, None, self.weight_decay)
        # 128
        x, nb_filter = _transition_block(x, 128, self.dropout_rate, 2, self.weight_decay)

        # 128 + 8 * 8 = 192
        x, nb_filter = _dense_block(x, 8, nb_filter, 8, None, self.weight_decay)
        # 192->128
        x, nb_filter = _transition_block(x, 128, self.dropout_rate, 2, self.weight_decay)

        # 128 + 8 * 8 = 192
        x, nb_filter = _dense_block(x, 8, nb_filter, 8, None, self.weight_decay)

        x = BatchNormalization(axis=-1, epsilon=1.1e-5)(x)
        x = Activation('relu')(x)

        x = Permute((2, 1, 3), name='permute')(x)
        x = TimeDistributed(Flatten(), name='flatten')(x)
        y_pred = Dense(self.num_classes, name='out', activation='softmax')(x)

        base_model = Model(inputs=input, outputs=y_pred)

        labels = Input(shape=(self.maxlen,), dtype='float32', name="the_labels")
        input_length = Input(shape=(1,), name="input_length", dtype='int64')
        label_length = Input(shape=(1,), name="label_length", dtype='int64')

        loss_out = Lambda(_ctc_loss, output_shape=(1,), name='ctc')([labels, y_pred, input_length, label_length])

        model = Model(inputs=[input, labels, input_length, label_length], outputs=loss_out)

        parallel_model = model
        if self.num_gpu > 1:
            parallel_model = multi_gpu_model(model, gpus=self.num_gpu)

        adam = Adam(self.lr)
        parallel_model.compile(loss={'ctc': lambda y_true, y_pred: y_pred}, optimizer=adam, metrics=['accuracy'])

        return base_model, model, parallel_model

    def train(self, epochs, train_data_loader: DataLoader, valid_data_loader: DataLoader, **kwargs):
        self.parallel_model.fit_generator(generator=train_data_loader.load_data(), epochs=epochs,
                                 steps_per_epoch=train_data_loader.steps_per_epoch,
                                 validation_data=valid_data_loader.load_data(),
                                 validation_steps=valid_data_loader.steps_per_epoch,
                                 **kwargs)

    def predict(self, image, id_to_char):
        if type(image) == str:
            img = Image.open(image)
        else:
            img = image
        im = img.convert('L')
        scale = im.size[1] * 1.0 / 32
        w = im.size[0] / scale
        w = int(w)

        im = im.resize((w, 32), Image.ANTIALIAS)
        img = np.array(im).astype(np.float32) / 255.0 - 0.5
        X = img.reshape((32, w, 1))
        X = np.array([X])

        y_pred = self.base_model.predict(X)
        argmax = np.argmax(y_pred, axis=2)[0]

        y_pred = y_pred[:, :, :]
        out = K.get_value(K.ctc_decode(y_pred, input_length=np.ones(y_pred.shape[0]) * y_pred.shape[1], )[0][0])[:, :]
        out = u''.join([id_to_char[x] for x in out[0]])

        return out, im

    @staticmethod
    def save_config(obj, config_path: str):
        with open(config_path, 'w+') as outfile:
            json.dump(obj.config(), outfile)

    @staticmethod
    def load_config(config_path: str):
        with open(config_path, 'r') as infile:
            return dict(json.load(infile))
