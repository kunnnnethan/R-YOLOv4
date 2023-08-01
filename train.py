import time
import random
import numpy as np
import torch
import os
import shutil
import json
import tqdm
import yaml
import argparse

from model.yolo import Yolo
from lib.load import load_data
from lib.scheduler import one_cycle
from lib.logger import Logger, logger
from lib.loss import ComputeLoss
from torch.optim.lr_scheduler import  LambdaLR
from test import test


def init():
    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def weights_init_normal(m):
    if isinstance(m, torch.nn.Conv2d):
        torch.nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif isinstance(m, torch.nn.BatchNorm2d):
        torch.nn.init.normal_(m.weight.data, 1.0, 0.02)
        torch.nn.init.constant_(m.bias.data, 0.0)


def fitness(x):
    # Model fitness as a weighted combination of metrics
    w = [0.0, 0.0, 0.1, 0.9]  # weights for [P, R, mAP@0.5, mAP@0.5:0.95]
    return (x * w).sum(0)


class Train:
    def __init__(self, args):
        self.args = args
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model_path = os.path.join("weights", self.args.model_name)
        self.model = None
        self.logger = None

    def check_model_path(self):
        if os.path.exists(self.model_path):
            while True:
                logger.warning("Model name exists, do you want to override the previous model?")
                inp = input(">> [Y:N]")
                if inp.lower()[0] == "y":
                    shutil.rmtree(self.model_path)
                    break
                elif inp.lower()[0] == "n":
                    logger.info("Stop training!")
                    exit(0)
                    
        os.makedirs(self.model_path)
        os.makedirs(os.path.join(self.model_path, "logs"))

    def load_model(self, n_classes, model_config):
        pretrained_dict = torch.load(self.args.weights_path)
        self.model = Yolo(n_classes, model_config)
        self.model = self.model.to(self.device)
        model_dict = self.model.state_dict()

        # 1. filter out unnecessary keys
        # 第552項開始為yololayer，訓練時不需要用到
        # pretrained_dict = {k: v for k, v in pretrained_dict.items() if np.shape(model_dict[k]) == np.shape(v)}
        pretrained_dict = {k: v for i, (k, v) in enumerate(pretrained_dict.items()) if i < 552}
        # 2. overwrite entries in the existing state dict
        model_dict.update(pretrained_dict)
        # 3. load the new state dict
        self.model.apply(weights_init_normal)  # 權重初始化
        self.model.load_state_dict(model_dict)

    def save_model(self, weightname):
        save_folder = os.path.join(self.model_path, "{}.pth".format(weightname))
        torch.save(self.model.state_dict(), save_folder)

    def save_opts(self, config):
        """Save options to disk so we know what we ran this experiment with
        """
        to_save = self.args.__dict__.copy()
        to_save.update(config)
        with open(os.path.join(self.model_path, 'opt.json'), 'w') as f:
            json.dump(to_save, f, indent=2)
    
    def logging_processes(self, epoch, total_train_loss, total_val_loss, mr, mp , map50, map5095, lr):
        tensorboard_log = {}
        
        # log training loss
        for name, loss in total_train_loss.items():
            tensorboard_log[f"train/{name}"] = loss

        # log validation loss
        for name, loss in total_val_loss.items():
            tensorboard_log[f"val/{name}"] = loss

        # log metrics
        tensorboard_log["metrics/mean recall"] = mr
        tensorboard_log["metrics/mean precision"] = mp
        tensorboard_log["metrics/mAP@.5"] = map50
        tensorboard_log["metrics/mAP@.5:.95"] = map5095
        tensorboard_log["lr"] = lr

        self.logger.list_of_scalars_summary(tensorboard_log, epoch)

    def train(self):
        # load data info
        with open(self.args.data, "r") as stream:
            data = yaml.safe_load(stream)

        # load configs
        with open(self.args.config, "r") as stream:
            config = yaml.safe_load(stream)
        hyp = config['hyp']

        self.check_model_path()
        self.load_model(len(data["names"]), config['model'])
        self.save_opts(config)
        self.logger = Logger(os.path.join(self.model_path, "logs"))

        train_dataset, train_dataloader = load_data(
            data['train'], data['names'], data['type'], hyp, self.args.img_size, self.args.batch_size, augment=True
        )
        num_iters_per_epoch = len(train_dataloader)

        nbs = 64  # nominal batch size
        accumulate = max(round(nbs / self.args.batch_size), 1)  # accumulate loss before optimizing

        optimizer = torch.optim.Adam(self.model.parameters(), lr=self.args.lr)

        nw =  max(int((self.args.epochs * num_iters_per_epoch) * hyp['warmup_prop']), 1000)
        lf = one_cycle(1, hyp['lrf'], int(self.args.epochs))
        scheduler = LambdaLR(optimizer, lr_lambda=lf)
        initial_lr = optimizer.param_groups[0]['initial_lr']

        compute_loss = ComputeLoss(self.model, hyp)

        logger.info(f'Image sizes {self.args.img_size}')
        logger.info(f'Starting training for {self.args.epochs} epochs...')
        
        best_fitness = 0

        for epoch in range(self.args.epochs):
            # -------------------
            # ------ Train ------
            # -------------------
            self.model.train()
            total_train_loss = {}
      
            logger.info(('\n' + '%10s' * 6) % ('Epoch', 'lr', 'box_loss', 'obj_loss', 'cls_loss', 'total'))
            pbar = enumerate(train_dataloader)
            pbar = tqdm.tqdm(pbar, total=len(train_dataloader))
            for batch, (_, imgs, targets) in pbar:
                global_step = num_iters_per_epoch * epoch + batch + 1
                imgs = imgs.to(self.device)
                targets = targets.to(self.device)

                # warmup
                if global_step <= nw:
                    xi = [0, nw]  # x interp
                    accumulate = max(1, np.interp(global_step, xi, [1, nbs / self.args.batch_size]).round())
                    optimizer.param_groups[0]['lr'] = np.interp(global_step, xi, [0.0, initial_lr * lf(epoch)])

                outputs = self.model(imgs, training=True)
                loss, loss_items = compute_loss(outputs, targets)

                loss.backward()

                if global_step % accumulate == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                
                # print info
                s = ('%10s'  + '%10.4g' * 5) % (
                    '%g/%g' % (epoch + 1, self.args.epochs), optimizer.param_groups[0]["lr"], loss_items["reg_loss"],
                    loss_items["conf_loss"], loss_items["cls_loss"], loss_items["total_loss"])
                # store loss items
                for item in loss_items:
                    if item in total_train_loss:
                        total_train_loss[item] += loss_items[item]
                    else:
                        total_train_loss[item] = loss_items[item]

                pbar.set_description(s)
                pbar.update(0)

            lr = optimizer.param_groups[0]["lr"] # for tensorboard
            scheduler.step()

            # -------------------
            # ------ Valid ------
            # -------------------
            mp, mr, map50, map5095, total_val_loss = test(
                self.model, compute_loss, self.device, data, hyp, 
                self.args.img_size, self.args.batch_size * 2, conf_thres=0.001, iou_thres=0.65
            )

            # average losses
            for item in total_train_loss:
                total_train_loss[item] /= len(train_dataloader)

            # update logging info for tensorboard every epoch  
            self.logging_processes(epoch, total_train_loss, total_val_loss, mr, mp , map50, map5095, lr)

            fit = fitness(np.array([mp, mr, map50, map5095]))
            if fit > best_fitness:
                best_fitness = fit
                self.save_model("best")
                logger.info("Current best model is saved!")
            self.save_model("last")

        logger.info("Done!")

        
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=80, help="number of epochs")
    parser.add_argument("--lr", type=float, default=0.001, help="learning rate")
    parser.add_argument("--batch_size", type=int, default=4, help="size of batches")
    parser.add_argument("--img_size", type=int, default=608, help="size of each image dimension")
    parser.add_argument("--weights_path", type=str, default="weights/yolov4.pth", help="path to pretrained weights file")
    parser.add_argument("--model_name", type=str, default="trash", help="new model name")
    parser.add_argument("--data", type=str, default="", help=".yaml path for data")
    parser.add_argument("--config", type=str, default="", help=".yaml path for configs")

    args = parser.parse_args()
    print(args)

    init()
    t = Train(args)
    t.train()
