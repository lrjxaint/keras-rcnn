import keras.backend
import keras.engine
import tensorflow

import keras_rcnn.backend
import keras_rcnn.layers

RPN_FG_FRACTION = 0.5
RPN_BATCHSIZE = 256


class AnchorTarget(keras.layers.Layer):
    """Calculate proposal anchor targets and corresponding labels (label: 1 is positive, 0 is negative, -1 is do not care) for ground truth boxes

    # Arguments
        allowed_border: allow boxes to be outside the image by allowed_border pixels
        clobber_positives: if an anchor statisfied by positive and negative conditions given to negative label
        negative_overlap: IoU threshold below which labels should be given negative label
        positive_overlap: IoU threshold above which labels should be given positive label

    # Input shape
        (# of batches, width of feature map, height of feature map, 2 * # of anchors), (# of samples, 4), (3)

    # Output shape
        (# of samples, ), (# of samples, 4)
    """

    def __init__(self, allowed_border=0, clobber_positives=False, negative_overlap=0.3, positive_overlap=0.7, stride=16, **kwargs):
        self.allowed_border = allowed_border
        self.clobber_positives = clobber_positives
        self.negative_overlap = negative_overlap
        self.positive_overlap = positive_overlap

        self.stride = stride

        super(AnchorTarget, self).__init__(**kwargs)

    def build(self, input_shape):
        super(AnchorTarget, self).build(input_shape)

    def call(self, inputs, **kwargs):
        scores, gt_boxes, metadata = inputs

        metadata = metadata[0,:]

        gt_boxes = gt_boxes[0]

        rr = keras.backend.shape(scores)[1]
        cc = keras.backend.shape(scores)[2]

        # 1. Generate proposals from bbox deltas and shifted anchors
        anchors = keras_rcnn.backend.shift((rr, cc), self.stride)       

        # 2. obtain indices of gt boxes with the greatest overlap, balanced labels
        argmax_overlaps_indices, labels = label(gt_boxes, anchors, self.negative_overlap, self.positive_overlap, self.clobber_positives)

        gt_boxes = keras.backend.gather(gt_boxes, argmax_overlaps_indices)

        # Convert fixed anchors in (x, y, w, h) to (dx, dy, dw, dh)
        bbox_reg_targets = keras_rcnn.backend.bbox_transform(anchors, gt_boxes)

        # reshape to (num_boxes, 4) 
        bbox_reg_targets = keras.backend.reshape(bbox_reg_targets, (-1, 4))
        
        # only keep anchors inside the image
        inds_inside = inside_image(anchors, metadata, self.allowed_border)
        labels = keras_rcnn.backend.where(inds_inside, labels, -1 * tensorflow.ones_like(labels))

        # expand the 0th axis as keras requires that exis for batch samples        
        labels = keras.backend.expand_dims(labels, axis=0)
        bbox_reg_targets = keras.backend.expand_dims(bbox_reg_targets, axis=0)

        # TODO: implement inside and outside weights
        return [labels, bbox_reg_targets]

    def compute_output_shape(self, input_shape):
        return [(None, None), (None, None, 4)]

    def compute_mask(self, inputs, mask=None):
        # unfortunately this is required
        return 2 * [None]


def balance(labels):
    """
    balance labels by setting some to -1
    :param labels: array of labels (1 is positive, 0 is negative, -1 is dont care)
    :return: array of labels
    """

    # subsample positive labels if we have too many
    labels = subsample_positive_labels(labels)

    # subsample negative labels if we have too many
    labels = subsample_negative_labels(labels)

    return labels


def label(y_true, y_pred, RPN_NEGATIVE_OVERLAP=0.3, RPN_POSITIVE_OVERLAP=0.7, clobber_positives=False):
    """
    Create bbox labels.
    label: 1 is positive, 0 is negative, -1 is do not care

    :param inds_inside: indices of anchors inside image
    :param y_pred: anchors
    :param y_true: ground truth objects

    :return: indices of gt boxes with the greatest overlap, balanced labels
    """

    ones = keras.backend.ones_like(y_pred[:,:1], dtype=keras.backend.floatx())
    labels = ones * -1
    zeros = keras.backend.zeros_like(y_pred[:,:1], dtype=keras.backend.floatx())

    argmax_overlaps_inds, max_overlaps, gt_argmax_overlaps_inds = overlapping(y_pred, y_true)

    # assign bg labels first so that positive labels can clobber them
    if not clobber_positives:
        labels = keras_rcnn.backend.where(keras.backend.less(max_overlaps, RPN_NEGATIVE_OVERLAP), zeros, labels)

    # fg label: for each gt, anchor with highest overlap

    # TODO: generalize unique beyond 1D
    unique_indices, unique_indices_indices = keras_rcnn.backend.unique(gt_argmax_overlaps_inds, return_index=True)
    inverse_labels = keras.backend.gather(-1 * labels, unique_indices)
    unique_indices = keras.backend.expand_dims(unique_indices, 1)
    updates = keras.backend.ones_like(keras.backend.reshape(unique_indices, (-1,1)), dtype=keras.backend.floatx())

    #return labels, unique_indices, inverse_labels, updates
    labels = keras_rcnn.backend.scatter_add_tensor(labels, unique_indices, inverse_labels + updates)
    # fg label: above threshold IOU
    labels = keras_rcnn.backend.where(keras.backend.greater_equal(max_overlaps, RPN_POSITIVE_OVERLAP), ones, labels)

    if clobber_positives:
        # assign bg labels last so that negative labels can clobber positives
        labels = keras_rcnn.backend.where(keras.backend.less(max_overlaps, RPN_NEGATIVE_OVERLAP), zeros, labels)
    
    labels = keras.backend.reshape(labels, (-1,))
    
    return argmax_overlaps_inds, balance(labels)


def overlapping(anchors, gt_boxes):
    """
    overlaps between the anchors and the gt boxes
    :param anchors: Generated anchors
    :param gt_boxes: Ground truth bounding boxes
    :param inds_inside:
    :return:
    """

    assert keras.backend.ndim(anchors) == 2
    assert keras.backend.ndim(gt_boxes) == 2

    reference = keras_rcnn.backend.overlap(anchors, gt_boxes)

    gt_argmax_overlaps_inds = keras.backend.argmax(reference, axis=0)

    argmax_overlaps_inds = keras.backend.argmax(reference, axis=1)

    arranged = keras.backend.arange(0, keras.backend.shape(anchors)[0])

    indices = keras.backend.stack([arranged, keras.backend.cast(argmax_overlaps_inds, "int32")], axis=0)

    indices = keras.backend.transpose(indices)

    max_overlaps = keras_rcnn.backend.gather_nd(reference, indices)

    return argmax_overlaps_inds, max_overlaps, gt_argmax_overlaps_inds


def subsample_negative_labels(labels):
    """
    subsample negative labels if we have too many
    :param labels: array of labels (1 is positive, 0 is negative, -1 is dont care)

    :return:
    """
    num_bg = RPN_BATCHSIZE - keras.backend.shape(keras_rcnn.backend.where(keras.backend.equal(labels, 1)))[0]

    bg_inds = keras_rcnn.backend.where(keras.backend.equal(labels, 0))

    num_bg_inds = keras.backend.shape(bg_inds)[0]

    size = num_bg_inds - num_bg

    def more_negative():
        indices = keras_rcnn.backend.shuffle(keras.backend.reshape(bg_inds, (-1,)))[:size]

        updates = tensorflow.ones((size,)) * -1

        inverse_labels = keras.backend.gather(labels, indices) * -1

        indices = keras.backend.reshape(indices, (-1, 1))

        return keras_rcnn.backend.scatter_add_tensor(labels, indices, inverse_labels + updates)

    condition = keras.backend.less_equal(size, 0)

    return keras.backend.switch(condition, labels, lambda: more_negative())


def subsample_positive_labels(labels):
    """
    subsample positive labels if we have too many
    :param labels: array of labels (1 is positive, 0 is negative, -1 is dont care)

    :return:
    """

    num_fg = int(RPN_FG_FRACTION * RPN_BATCHSIZE)

    fg_inds = keras_rcnn.backend.where(keras.backend.equal(labels, 1))
    num_fg_inds = keras.backend.shape(fg_inds)[0]

    size = num_fg_inds - num_fg

    def more_positive():
        # TODO: try to replace tensorflow
        indices = keras_rcnn.backend.shuffle(keras.backend.reshape(fg_inds, (-1,)))[:size]

        updates = tensorflow.ones((size,)) * -1

        inverse_labels = keras.backend.gather(labels, indices) * -1

        indices = keras.backend.reshape(indices, (-1, 1))

        return keras_rcnn.backend.scatter_add_tensor(labels, indices, inverse_labels + updates)

    condition = keras.backend.less_equal(size, 0)

    return keras.backend.switch(condition, labels, lambda: more_positive())



def inside_image(boxes, im_info, allowed_border=0):
    """
    Calc indices of boxes which are located completely inside of the image
    whose size is specified by img_info ((height, width, scale)-shaped array).

    :param boxes: (None, 4) tensor containing boxes in original image (x1, y1, x2, y2)
    :param img_info: (height, width, scale)
    :param allowed_border: allow boxes to be outside the image by allowed_border pixels
    :return: (None, 4) indices of boxes completely in original image,
        (None, 4) tensor of boxes completely inside image
    """

    indices = (
        (boxes[:, 0] >= -allowed_border) &
        (boxes[:, 1] >= -allowed_border) &
        (boxes[:, 2] < allowed_border + im_info[1]) & # width
        (boxes[:, 3] < allowed_border + im_info[0])   # height
    )

    return indices
