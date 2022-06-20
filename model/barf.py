import numpy as np
import os,sys,time
import torch
import torch.nn.functional as torch_F
import torchvision
import torchvision.transforms.functional as torchvision_F
import tqdm
from easydict import EasyDict as edict
import visdom
import matplotlib.pyplot as plt

import util,util_vis
from util import log,debug
from . import nerf
import camera
import imageio
import PIL

# ============================ main engine for training and evaluation ============================

class Model(nerf.Model):

    def __init__(self,opt):
        super().__init__(opt)

    def build_networks(self,opt):
        super().build_networks(opt)
        if opt.camera.noise:
            # pre-generate synthetic pose perturbation
            se3_noise = torch.randn(len(self.train_data),6,device=opt.device)*opt.camera.noise
            self.graph.pose_noise = camera.lie.se3_to_SE3(se3_noise)
        self.graph.se3_refine = torch.nn.Embedding(len(self.train_data),6).to(opt.device) #TODO : refine되는 포즈?, (n,6) : 6개로 나타내나봐
        torch.nn.init.zeros_(self.graph.se3_refine.weight) #(n,6) shape의 0으로 채워진 행렬

    def setup_optimizer(self,opt):
        super().setup_optimizer(opt)
        optimizer = getattr(torch.optim,opt.optim.algo)  #Adam
        self.optim_pose = optimizer([dict(params=self.graph.se3_refine.parameters(),lr=opt.optim.lr_pose)]) #TODO: optimize pose
        # set up scheduler
        if opt.optim.sched_pose:
            scheduler = getattr(torch.optim.lr_scheduler,opt.optim.sched_pose.type)
            if opt.optim.lr_pose_end:
                assert(opt.optim.sched_pose.type=="ExponentialLR")
                opt.optim.sched_pose.gamma = (opt.optim.lr_pose_end/opt.optim.lr_pose)**(1./opt.max_iter)
            kwargs = { k:v for k,v in opt.optim.sched_pose.items() if k!="type" }
            self.sched_pose = scheduler(self.optim_pose,**kwargs)

    def train_iteration(self,opt,var,loader):
        self.optim_pose.zero_grad()
        if opt.optim.warmup_pose:
            # simple linear warmup of pose learning rate
            self.optim_pose.param_groups[0]["lr_orig"] = self.optim_pose.param_groups[0]["lr"] # cache the original learning rate
            self.optim_pose.param_groups[0]["lr"] *= min(1,self.it/opt.optim.warmup_pose)
        loss = super().train_iteration(opt,var,loader)
        self.optim_pose.step()
        if opt.optim.warmup_pose:
            self.optim_pose.param_groups[0]["lr"] = self.optim_pose.param_groups[0]["lr_orig"] # reset learning rate
        if opt.optim.sched_pose: self.sched_pose.step()
        self.graph.nerf.progress.data.fill_(self.it/opt.max_iter)
        if opt.nerf.fine_sampling:
            self.graph.nerf_fine.progress.data.fill_(self.it/opt.max_iter)
        return loss

    @torch.no_grad()
    def log_scalars(self,opt,var,loss,metric=None,step=0,split="train"):
        super().log_scalars(opt,var,loss,metric=metric,step=step,split=split)
        if split=="train":
            # log learning rate
            lr = self.optim_pose.param_groups[0]["lr"]
            self.tb.add_scalar("{0}/{1}".format(split,"lr_pose"),lr,step) #
        # compute pose error
        if split=="train" and opt.data.dataset in ["iphone","arkit","blender","llff"]:
            pose,pose_GT = self.get_all_training_poses(opt)
            pose_aligned,_ = self.prealign_cameras(opt,pose,pose_GT)
            error = self.evaluate_camera_alignment(opt,pose_aligned,pose_GT)
            self.tb.add_scalar("{0}/error_R".format(split),error.R.mean(),step)  # Tensorboard
            self.tb.add_scalar("{0}/error_t".format(split),error.t.mean(),step)

    @torch.no_grad()
    def visualize(self,opt,var,step=0,split="train"):
        super().visualize(opt,var,step=step,split=split)
        if opt.visdom:
            if split=="val":
                pose,pose_GT = self.get_all_training_poses(opt)
                util_vis.vis_cameras(opt,self.vis,step=step,poses=[pose,pose_GT])

    @torch.no_grad()
    def get_all_training_poses(self,opt):
        # get ground-truth (canonical) camera poses
        # add synthetic pose perturbation to all training data
        if opt.data.dataset in ["blender"] :
            pose_GT = self.train_data.get_all_camera_poses(opt).to(opt.device)
            pose = pose_GT # self.train_data.get_all_camera_poses(opt).to(opt.device)  #(3,4)
            if opt.camera.noise:
                pose = camera.pose.compose([self.graph.pose_noise,pose])
        elif opt.data.dataset in ["arkit"] :
            pose_GT = self.train_data.get_all_gt_camera_poses(opt).to(opt.device)  # (3,4)
            pose = self.train_data.get_all_camera_poses(opt).to(opt.device)  #initial pose
        else:
            pose_GT = self.train_data.get_all_gt_camera_poses(opt).to(opt.device)  # (3,4)
            pose = self.graph.pose_eye
        # add learned pose correction to all training data
        pose_refine = camera.lie.se3_to_SE3(self.graph.se3_refine.weight) #embeding
        pose = camera.pose.compose([pose_refine,pose]) #refine_pose와 pose 사이 pose_new(x) = poseN o ... o pose2 o pose1(x) 이렇게
        return pose,pose_GT

    @torch.no_grad()
    def get_all_optitrack_training_poses(self,opt):
        # get ground-truth (canonical) camera poses
        # add synthetic pose perturbation to all training data
        if opt.data.dataset in ["blender"] :
            pose_GT = self.train_data.get_all_camera_poses(opt).to(opt.device)
            pose = pose_GT # self.train_data.get_all_camera_poses(opt).to(opt.device)  #(3,4)
            if opt.camera.noise:
                pose = camera.pose.compose([self.graph.pose_noise,pose])
        elif opt.data.dataset in ["arkit"] :
            pose_GT = self.train_data.get_all_optitrack_camera_poses(opt).to(opt.device)  # (3,4) optitrack
            pose = self.train_data.get_all_camera_poses(opt).to(opt.device)  #initial pose
        else:
            pose_GT = self.train_data.get_all_optitrack_camera_poses(opt).to(opt.device)  # (3,4) optitrack
            pose = self.graph.pose_eye
        # add learned pose correction to all training data
        pose_refine = camera.lie.se3_to_SE3(self.graph.se3_refine.weight) #embeding
        pose = camera.pose.compose([pose_refine,pose]) #refine_pose와 pose 사이 pose_new(x) = poseN o ... o pose2 o pose1(x) 이렇게
        return pose,pose_GT




    @torch.no_grad()
    def prealign_cameras(self,opt,pose,pose_GT):
        # compute 3D similarity transform via Procrustes analysis
        center = torch.zeros(1,1,3,device=opt.device)
        center_pred = camera.cam2world(center,pose)[:,0] # [N,3]
        center_GT = camera.cam2world(center,pose_GT)[:,0] # [N,3]
        try:
            sim3 = camera.procrustes_analysis(center_GT,center_pred)
        except:
            print("warning: SVD did not converge...")
            sim3 = edict(t0=0,t1=0,s0=1,s1=1,R=torch.eye(3,device=opt.device))
        # align the camera poses
        center_aligned = (center_pred-sim3.t1)/sim3.s1@sim3.R.t()*sim3.s0+sim3.t0
        R_aligned = pose[...,:3]@sim3.R.t()
        t_aligned = (-R_aligned@center_aligned[...,None])[...,0]
        pose_aligned = camera.pose(R=R_aligned,t=t_aligned)
        return pose_aligned,sim3

    @torch.no_grad()
    def evaluate_camera_alignment(self,opt,pose_aligned,pose_GT):
        # measure errors in rotation and translation
        R_aligned,t_aligned = pose_aligned.split([3,1],dim=-1) #TODO:shape
        R_GT,t_GT = pose_GT.split([3,1],dim=-1)
        R_error = camera.rotation_distance(R_aligned,R_GT)
        t_error = (t_aligned-t_GT)[...,0].norm(dim=-1)
        error = edict(R=R_error,t=t_error)
        return error

    @torch.no_grad()
    def evaluate_full(self,opt):
        self.graph.eval()
        # evaluate rotation/translation
        #TODO : GT optitrack pose data load
        pose, pose_GT = self.get_all_optitrack_training_poses(opt) # train 과정에서 optimize한 포즈 범위, GT pose

        pose_aligned,self.graph.sim3 = self.prealign_cameras(opt,pose,pose_GT)
        error = self.evaluate_camera_alignment(opt,pose_aligned,pose_GT)
        print("--------------------------")
        print("rot:   {:8.3f}".format(np.rad2deg(error.R.mean().cpu())))
        print("trans: {:10.5f}".format(error.t.mean()))
        print("--------------------------")
        # dump numbers
        quant_fname = "{}/quant_pose.txt".format(opt.output_path)
        with open(quant_fname,"w") as file:
            for i,(err_R,err_t) in enumerate(zip(error.R,error.t)):
                file.write("{} {} {}\n".format(i,err_R.item(),err_t.item()))
        # evaluate novel view synthesis
        super().evaluate_full(opt)

    @torch.enable_grad()
    def evaluate_test_time_photometric_optim(self,opt,var):
        # use another se3 Parameter to absorb the remaining pose errors
        var.se3_refine_test = torch.nn.Parameter(torch.zeros(1,6,device=opt.device))
        optimizer = getattr(torch.optim,opt.optim.algo)
        optim_pose = optimizer([dict(params=[var.se3_refine_test],lr=opt.optim.lr_pose)])
        iterator = tqdm.trange(opt.optim.test_iter,desc="test-time optim.",leave=False,position=1)
        for it in iterator:
            optim_pose.zero_grad()
            var.pose_refine_test = camera.lie.se3_to_SE3(var.se3_refine_test)
            var = self.graph.forward(opt,var,mode="test-optim")
            loss = self.graph.compute_loss(opt,var,mode="test-optim")
            loss = self.summarize_loss(opt,var,loss)
            loss.all.backward()
            optim_pose.step()
            iterator.set_postfix(loss="{:.3f}".format(loss.all))
        return var

    @torch.no_grad()
    def generate_videos_pose(self,opt):
        self.graph.eval()
        fig = plt.figure(figsize=(10,10) if opt.data.dataset in ["blender"] else (16,8))
        cam_path = "{}/poses".format(opt.output_path)
        os.makedirs(cam_path,exist_ok=True)
        ep_list = []
        for ep in range(0,opt.max_iter+1,opt.freq.ckpt): # 5000 간격으로
            # load checkpoint (0 is random init)
            if ep!=0:
                try: util.restore_checkpoint(opt,self,resume=ep)
                except: continue
            # get the camera poses
            pose,pose_ref = self.get_all_training_poses(opt) #pose_ref == GT
            if opt.data.dataset in ["iphone","arkit","blender","llff"]:
                pose_aligned,_ = self.prealign_cameras(opt,pose,pose_ref)
                pose_aligned,pose_ref = pose_aligned.detach().cpu(),pose_ref.detach().cpu()
                dict(
                    blender=util_vis.plot_save_poses_blender,
                    llff=util_vis.plot_save_poses,
                    arkit=util_vis.plot_save_poses,
                    iphone=util_vis.plot_save_poses,
                )[opt.data.dataset](opt,fig,pose_aligned,pose_ref=pose_ref,path=cam_path,ep=ep)
            else:
                pose = pose.detach().cpu()  # 여기서 원본이랑 보정된 포즈 다 그리지말고 몇개당 하나만 추출해서 그리자
                util_vis.plot_save_poses(opt,fig,pose,pose_ref=None,path=cam_path,ep=ep)
            ep_list.append(ep)
        plt.close()
        # write videos
        print("writing videos...")
        list_fname = "{}/temp.list".format(cam_path)
        with open(list_fname,"w") as file:
            for ep in ep_list: file.write("file {}.png\n".format(ep))
        cam_vid_fname = "{}/poses.mp4".format(opt.output_path)
        os.system("ffmpeg -y -r 30 -f concat -i {0} -pix_fmt yuv420p {1} >/dev/null 2>&1".format(list_fname,cam_vid_fname))
        os.remove(list_fname)

        pose_img = []
        for ep in ep_list:
            pose_image_name = "{}/{}.png".format(cam_path, ep)
            pose_img.append(PIL.Image.fromarray(imageio.imread(pose_image_name)))
        imageio.mimwrite(os.path.join(opt.output_path, 'poses.gif'), pose_img, fps=60)

    """ 논문에 넣을 select한 포즈만 그리기 위한 파트 """
    @torch.no_grad()
    def generate_optim_pose(self,opt):
        self.graph.eval()
        fig = plt.figure(figsize=(10,10) if opt.data.dataset in ["blender"] else (16,8))
        cam_path = "{}/poses_iter200000".format(opt.output_path)
        os.makedirs(cam_path,exist_ok=True)
        ep_list = []
        ep = 200000
        if ep != 0:
            try:
                util.restore_checkpoint(opt, self, resume=ep)
            except:
                return

            # get the camera poses
        pose,pose_ref = self.get_all_training_poses(opt) #pose_ref == GT
        # TODO: 평균 위치 빼서 중앙으로 옮기기
        N = pose.shape[0]
        plot_list_index = [i for i in range(N)]
        #TODO : 튕긴 값들 추가해주기
        plot_list_index = plot_list_index.sort()
        if opt.data.dataset in ["iphone","arkit","blender","llff"]:
            pose_aligned,_ = self.prealign_cameras(opt,pose,pose_ref)
            pose_aligned,pose_ref = pose_aligned.detach().cpu(),pose_ref.detach().cpu()
            dict(
                blender=util_vis.plot_save_poses_blender,
                llff=util_vis.plot_save_poses,
                arkit=util_vis.plot_save_poses,
                iphone=util_vis.plot_save_poses,
            )[opt.data.dataset](opt,fig,pose_aligned,pose_ref=pose_ref,path=cam_path,ep=ep)
        else:
            pose = pose.detach().cpu()
            util_vis.plot_save_poses(opt,fig,pose,pose_ref=None,path=cam_path,ep=ep)
        ep_list.append(ep)
        plt.close()
        # write videos
        print("writing videos...")
        list_fname = "{}/temp.list".format(cam_path)
        with open(list_fname,"w") as file:
            for ep in ep_list: file.write("file {}.png\n".format(ep))
        cam_vid_fname = "{}/poses.mp4".format(opt.output_path)
        os.system("ffmpeg -y -r 30 -f concat -i {0} -pix_fmt yuv420p {1} >/dev/null 2>&1".format(list_fname,cam_vid_fname))
        os.remove(list_fname)

        pose_img = []
        for ep in ep_list:
            pose_image_name = "{}/{}.png".format(cam_path, ep)
            pose_img.append(PIL.Image.fromarray(imageio.imread(pose_image_name)))
        imageio.mimwrite(os.path.join(opt.output_path, 'poses.gif'), pose_img, fps=60)


    """ train data pose 하나씩 그려서 튕긴 데이터 찾기 위한 코드
        논문에 넣을 튕긴 데이터 찾는 코드
    """
    @torch.no_grad()
    def generate_optim_pose_onebyone(self, opt):
        self.graph.eval()
        fig = plt.figure(figsize=(10, 10) if opt.data.dataset in ["blender"] else (16, 8))
        cam_path = "{}/poses_onebyone".format(opt.output_path)
        os.makedirs(cam_path, exist_ok=True)
        ep_list = []
        ep = 200000
        if ep != 0:
            try:
                util.restore_checkpoint(opt, self, resume=ep)
            except:
                return

            # get the camera poses
        pose, pose_ref = self.get_all_training_poses(opt)  # pose_ref == GT
        N = pose.shape[0]
        pose_aligned, _ = self.prealign_cameras(opt, pose, pose_ref)
        pose_aligned, pose_ref = pose_aligned.detach().cpu(), pose_ref.detach().cpu()
        for i in range(N):
            if opt.data.dataset in ["iphone", "arkit", "blender", "llff"]:
                # pose_aligned, _ = self.prealign_cameras(opt, pose, pose_ref)
                # pose_aligned, pose_ref = pose_aligned.detach().cpu(), pose_ref.detach().cpu()

                dict(
                    blender=util_vis.plot_save_poses_blender,
                    llff=util_vis.plot_save_poses,
                    arkit=util_vis.plot_save_poses,
                    iphone=util_vis.plot_save_poses,
                )[opt.data.dataset](opt, fig, pose_aligned[i], pose_ref=pose_ref[i], path=cam_path, ep=ep)
            else:
                pose = pose.detach().cpu()  # 여기서 원본이랑 보정된 포즈 다 그리지말고 몇개당 하나만 추출해서 그리자
                util_vis.plot_save_poses(opt, fig,  pose[i], pose_ref=None, path=cam_path, ep=ep)
            ep_list.append(i)
        plt.close()
        # write videos
        print("writing videos...")
        list_fname = "{}/temp.list".format(cam_path)
        with open(list_fname, "w") as file:
            for ep in ep_list: file.write("file {}.png\n".format(ep))
        cam_vid_fname = "{}/poses.mp4".format(opt.output_path)
        os.system(
            "ffmpeg -y -r 30 -f concat -i {0} -pix_fmt yuv420p {1} >/dev/null 2>&1".format(list_fname, cam_vid_fname))
        os.remove(list_fname)

        pose_img = []
        for ep in ep_list:
            pose_image_name = "{}/{}.png".format(cam_path, ep)
            pose_img.append(PIL.Image.fromarray(imageio.imread(pose_image_name)))


    @torch.no_grad()
    def evaluate_ckt(self, opt):
        log.info("evaluate ckpt pose...")
        self.graph.eval()
        # 매 이터레이션마다 train pose의 ATE 평균값 계산 후 평균내서 텍스트 파일로
        pose_err_list = []  # ate는 아닌데 pose,
        for ep in range(0, opt.max_iter + 1, opt.freq.ckpt):  # 5000 간격으로
            # load checkpoint (0 is random init)
            if ep != 0:
                try:
                    util.restore_checkpoint(opt, self, resume=ep)
                except:
                    continue
            # evaluate rotation/translation
            pose, pose_GT = self.get_all_training_poses(opt)
            pose_aligned, self.graph.sim3 = self.prealign_cameras(opt, pose, pose_GT)
            error = self.evaluate_camera_alignment(opt, pose_aligned, pose_GT)
            rot = np.rad2deg(error.R.mean().cpu())
            trans = error.t.mean()
            pose_err_list.append(edict(ep=ep, rot=rot, trans=trans))
        ckpt_ate_fname = "{}/ckpt_quant_pose.txt".format(opt.output_path)
        with open(ckpt_ate_fname, "w") as file:
            for i,list in enumerate(pose_err_list):
                file.write("{} {} {}\n".format(list.ep, list.rot, list.trans))
        # nerf.py의 eval_everyiter로 접근
        super().evaluate_ckt(opt)

# ============================ computation graph for forward/backprop ============================

class Graph(nerf.Graph):

    def __init__(self,opt):
        super().__init__(opt)
        self.nerf = NeRF(opt)
        if opt.nerf.fine_sampling:
            self.nerf_fine = NeRF(opt)
        self.pose_eye = torch.eye(3,4).to(opt.device)

    def get_pose(self,opt,var,mode=None):
        if mode=="train":
            # add the pre-generated pose perturbations
            if opt.data.dataset in ["arkit","blender"] :
                if opt.camera.noise:
                    var.pose_noise = self.pose_noise[var.idx]
                    pose = camera.pose.compose([var.pose_noise,var.pose])
                else: pose = var.pose
            else: pose = self.pose_eye
            # add learnable pose correction
            var.se3_refine = self.se3_refine.weight[var.idx] #Embedding(n,6)
            pose_refine = camera.lie.se3_to_SE3(var.se3_refine) #Embedding(n,6)
            pose = camera.pose.compose([pose_refine,pose])  #(n,3,4)
            # print('### se3_refine : {}'.format(self.se3_refine))
            # print('### pose_refine  shape : {}'.format(self.se3_refine))
            # print('### pose  shape: {}'.format(pose.shape))

        elif mode in ["eval","test-optim"]:
            # align test pose to refined coordinate system (up to sim3)
            sim3 = self.sim3
            center = torch.zeros(1,1,3,device=opt.device)
            center = camera.cam2world(center,var.pose)[:,0] # [N,3]
            center_aligned = (center-sim3.t0)/sim3.s0@sim3.R*sim3.s1+sim3.t1
            R_aligned = var.pose[...,:3]@self.sim3.R
            t_aligned = (-R_aligned@center_aligned[...,None])[...,0]
            pose = camera.pose(R=R_aligned,t=t_aligned)
            # additionally factorize the remaining pose imperfection
            if opt.optim.test_photo:
                pose = camera.pose.compose([var.pose_refine_test,pose])
        else: pose = var.pose
        return pose

class NeRF(nerf.NeRF):

    def __init__(self,opt):
        super().__init__(opt)
        self.progress = torch.nn.Parameter(torch.tensor(0.)) # use Parameter so it could be checkpointed

    def positional_encoding(self,opt,input,L): # [B,...,N]
        input_enc = super().positional_encoding(opt,input,L=L) # [B,...,2NL]
        # coarse-to-fine: smoothly mask positional encoding for BARF
        if opt.barf_c2f is not None:
            # set weights for different frequency bands
            start,end = opt.barf_c2f
            alpha = (self.progress.data-start)/(end-start)*L
            k = torch.arange(L,dtype=torch.float32,device=opt.device)
            weight = (1-(alpha-k).clamp_(min=0,max=1).mul_(np.pi).cos_())/2
            # apply weights
            shape = input_enc.shape
            input_enc = (input_enc.view(-1,L)*weight).view(*shape)
        return input_enc
