import math
import torch
import torch.nn as nn
import torch.nn.functional as F


class FocalLoss(nn.Module):
    # Reference: https://github.com/ultralytics/yolov5/blob/8918e6347683e0f2a8a3d7ef93331001985f6560/utils/loss.py#L32
    def __init__(self, alpha=0.25, gamma=2, reduction="none"):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.alpha = alpha
        self.reduction = reduction

    def forward(self, inputs, targets):
        loss = F.binary_cross_entropy(inputs, targets, reduction='none')
        p_t = targets * inputs + (1 - targets) * (1 - inputs)
        alpha_factor = targets * self.alpha + (1 - targets) * (1 - self.alpha)
        modulating_factor = (1.0 - p_t) ** self.gamma
        loss *= alpha_factor * modulating_factor

        if self.reduction == 'mean':
            loss = loss.mean()
        elif self.reduction == 'sum':
            loss = loss.sum()
        return loss


def bbox_xywha_ciou(pred_boxes, target_boxes):
    # Reference: https://github.com/Zzh-tju/DIoU-SSD-pytorch/blob/86a370aa2cadea6ba7e5dffb2efc4bacc4c863ea/
    #            utils/box/box_utils.py#L47
    """
    :param pred_boxes: [num_of_objects, 4], boxes predicted by yolo and have been scaled
    :param target_boxes: [num_of_objects, 4], ground truth boxes and have been scaled
    :return: ciou loss
    """
    assert pred_boxes.size() == target_boxes.size()

    # xywha -> xyxya
    # xy is center point, so to get the former x of the bbox, you need to minus the 0.5 * width or height
    pred_boxes = torch.cat(
        [pred_boxes[..., :2] - pred_boxes[..., 2:4] / 2, 
         pred_boxes[..., :2] + pred_boxes[..., 2:4] / 2,
         pred_boxes[..., 4:]], dim=-1)
    target_boxes = torch.cat(
        [target_boxes[..., :2] - target_boxes[..., 2:4] / 2,
         target_boxes[..., :2] + target_boxes[..., 2:4] / 2,
         target_boxes[..., 4:]], dim=-1)

    w1 = pred_boxes[:, 2] - pred_boxes[:, 0] # x2 - x1
    h1 = pred_boxes[:, 3] - pred_boxes[:, 1] # y2 - y1
    w2 = target_boxes[:, 2] - target_boxes[:, 0]
    h2 = target_boxes[:, 3] - target_boxes[:, 1]

    area1 = w1 * h1
    area2 = w2 * h2

    center_x1 = (pred_boxes[:, 2] + pred_boxes[:, 0]) / 2
    center_y1 = (pred_boxes[:, 3] + pred_boxes[:, 1]) / 2
    center_x2 = (target_boxes[:, 2] + target_boxes[:, 0]) / 2
    center_y2 = (target_boxes[:, 3] + target_boxes[:, 1]) / 2

    inter_max_xy = torch.min(pred_boxes[:, 2:4], target_boxes[:, 2:4])
    inter_min_xy = torch.max(pred_boxes[:, :2], target_boxes[:, :2])
    out_max_xy = torch.max(pred_boxes[:, 2:4], target_boxes[:, 2:4])
    out_min_xy = torch.min(pred_boxes[:, :2], target_boxes[:, :2])

    inter = torch.clamp((inter_max_xy - inter_min_xy), min=0)
    inter_area = inter[:, 0] * inter[:, 1]
    inter_diag = (center_x2 - center_x1) ** 2 + (center_y2 - center_y1) ** 2
    outer = torch.clamp((out_max_xy - out_min_xy), min=0)
    outer_diag = (outer[:, 0] ** 2) + (outer[:, 1] ** 2) # c ^ 2
    union = area1 + area2 - inter_area
    u = inter_diag / (outer_diag + 1e-15)

    iou = inter_area / (union + 1e-15)
    v = (4 / (math.pi ** 2)) * torch.pow((torch.atan(w2 / h2) - torch.atan(w1 / h1)), 2)

    # alpha is a constant, it don't have gradient
    with torch.no_grad():
        S = 1 - iou
        alpha = v / (S + v)

    ciou_loss = iou - (u + alpha * v)
    ciou_loss = torch.clamp(ciou_loss, min=-1.0, max=1.0)

    angle_factor = torch.abs(torch.cos(pred_boxes[:, 4] - target_boxes[:, 4]))
    # skew_iou = torch.abs(iou * angle_factor) + 1e-16
    skew_iou = iou * angle_factor
    return skew_iou, ciou_loss


class ComputeLoss:
    def __init__(self, hyp, device):
        self.focal_loss = FocalLoss(gamma=hyp['fl_gamma'], reduction=self.reduction)

    def __call__(self):
        iou_scores, skew_iou, ciou_loss, class_mask, obj_mask, noobj_mask, ta, tcls, tconf = self.build_targets(
            pred_boxes=pred_boxes, pred_cls=pred_cls, target=target, masked_anchors=masked_anchors
        )
        # --------------------
        # - Calculating Loss -
        # --------------------
        reg_loss, conf_loss, cls_loss = torch.zeros(1, device=device), torch.zeros(1, device=device), torch.zeros(1, device=device)
        FOCAL = FocalLoss(reduction=self.reduction)

        if len(target) > 0:
            # Reg Loss for bounding box prediction
            iou_const = skew_iou[obj_mask]
            angle_loss = F.smooth_l1_loss(pred_a[obj_mask], ta[obj_mask], reduction="none")
            reg_vector = angle_loss + ciou_loss[obj_mask]
            with torch.no_grad():
                reg_magnitude = iou_const / reg_vector
            reg_loss += (reg_magnitude * reg_vector).mean()

            # Focal Loss for object's prediction
            conf_loss += FOCAL(pred_conf[obj_mask], tconf[obj_mask])

            # Binary Cross Entropy Loss for class' prediction
            cls_loss += F.binary_cross_entropy(pred_cls[obj_mask], tcls[obj_mask], reduction=self.reduction)

        conf_loss += FOCAL(pred_conf[noobj_mask], tconf[noobj_mask])

        # Loss scaling
        reg_loss = self.lambda_coord * reg_loss
        conf_loss = self.lambda_conf_scale * conf_loss
        cls_loss = self.lambda_cls_scale * cls_loss
        loss = reg_loss + conf_loss + cls_loss

        # --------------------
        # -   Logging Info   -
        # --------------------
        #cls_acc = 100 * class_mask[obj_mask].mean()
        #conf50 = (pred_conf > 0.5).float()
        #iou50 = (iou_scores > 0.5).float()
        #iou75 = (iou_scores > 0.75).float()
        #detected_mask = conf50 * class_mask * tconf
        #precision = torch.sum(iou50 * detected_mask) / (conf50.sum() + 1e-16)
        #recall50 = torch.sum(iou50 * detected_mask) / (obj_mask.sum() + 1e-16)
        #recall75 = torch.sum(iou75 * detected_mask) / (obj_mask.sum() + 1e-16)

        loss_items = {
            "loss": to_cpu(loss).item(),
            "reg_loss": to_cpu(reg_loss).item(),
            "conf_loss": to_cpu(conf_loss).item(),
            "cls_loss": to_cpu(cls_loss).item(),
        }

        return output, loss, loss_items

    def build_targets(self, output, target):
        # output.shape -> [batch, num_bboxes, 8]
        pred_boxes = output[..., :5]
        pred_cls = output[..., 6:]
        
        # num of (batches, anchors(3*6), downsample grid sizes, _ , classes)
        nB, nA, nG, _, nC = pred_cls.size()
        device = pred_boxes.device

        # Output tensors
        obj_mask = torch.zeros((nB, nA, nG, nG), device=device)
        noobj_mask = torch.ones((nB, nA, nG, nG), device=device)
        class_mask = torch.zeros((nB, nA, nG, nG), device=device)
        iou_scores = torch.zeros((nB, nA, nG, nG), device=device)
        skew_iou = torch.zeros((nB, nA, nG, nG), device=device)
        ciou_loss = torch.zeros((nB, nA, nG, nG), device=device)
        ta = torch.zeros((nB, nA, nG, nG), device=device)
        tcls = torch.zeros((nB, nA, nG, nG, nC), device=device)

        # Convert ground truth position to position that relative to the size of box (grid size)

        # target_boxes(x,y,w,h,a,...(classes))
        target_boxes = torch.cat((target[:, 2:6] * nG, target[:, 6:]), dim=-1)#(originally normalize w.r.t grids)

        gxy = target_boxes[:, :2]
        gwh = target_boxes[:, 2:4]
        ga = target_boxes[:, 4]

        # Get anchors with best iou and their angle difference with ground truths
        arious = []
        offset = []
        with torch.no_grad():
            for anchor in masked_anchors:
                ariou = anchor_wh_iou(anchor[:2], gwh)
                cos = torch.abs(torch.cos(torch.sub(anchor[2], ga)))
                arious.append(ariou * cos)
                offset.append(torch.abs(torch.sub(anchor[2], ga)))
            arious = torch.stack(arious)
            offset = torch.stack(offset)
        best_ious, best_n = arious.max(0)

        # Separate target values
        # b indicates which batch, target_labels is the class label (0 or 1)
        b, target_labels = target[:, :2].long().t()
        gi, gj = gxy.long().t()

        # Avoid the error caused by the wrong position of the center coordinate of objects
        gi = torch.clamp(gi, 0, nG - 1)
        gj = torch.clamp(gj, 0, nG - 1)

        # Set masks to specify object's location
        # for img the row is y and col is x
        obj_mask[b, best_n, gj, gi] = 1
        noobj_mask[b, best_n, gj, gi] = 0

        # TODO :: verify that the code here is correct
        # Set noobj mask to zero where iou exceeds ignore threshold
        for i, (anchor_ious, angle_offset) in enumerate(zip(arious.t(), offset.t())):
            noobj_mask[b[i], (anchor_ious > self.ignore_thresh), gj[i], gi[i]] = 0
            # if iou is greater than 0.4 and the angle offset if smaller than 15 degrees then ignore training
            noobj_mask[b[i], (anchor_ious > 0.4) & (angle_offset < (np.pi / 12)), gj[i], gi[i]] = 0

        # Angle (encode)
        ta[b, best_n, gj, gi] = ga - masked_anchors[best_n][:, 2]

        # One-hot encoding of label
        tcls[b, best_n, gj, gi, target_labels] = 1
        tconf = obj_mask.float()

        # Calculate ciou loss
        iou, ciou = bbox_xywha_ciou(pred_boxes[b, best_n, gj, gi], target_boxes)
        with torch.no_grad():
            img_size = self.stride * nG
            bbox_loss_scale = 2.0 - 1.0 * gwh[:, 0] * gwh[:, 1] / (img_size ** 2)
        ciou = bbox_loss_scale * (1.0 - ciou)

        # magnitude for reg loss
        skew_iou[b, best_n, gj, gi] = torch.exp(1 - iou) - 1

        # unit vector for reg loss
        ciou_loss[b, best_n, gj, gi] = ciou

        # Compute label correctness and iou at best anchor
        class_mask[b, best_n, gj, gi] = (pred_cls[b, best_n, gj, gi].argmax(-1) == target_labels).float()
        iou_scores[b, best_n, gj, gi] = iou.detach()

        obj_mask = obj_mask.type(torch.bool)
        noobj_mask = noobj_mask.type(torch.bool)

        return iou_scores, skew_iou, ciou_loss, class_mask, obj_mask, noobj_mask, ta, tcls, tconf