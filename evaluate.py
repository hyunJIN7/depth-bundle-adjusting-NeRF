import numpy as np
import os,sys,time
import torch
import importlib
import options
from util import log
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
# os.environ["CUDA_VISIBLE_DEVICES"] = "0"

# python3 evaluate.py --group=strayscanner or iphone
#                     --model=barf
#                     --yaml=barf_strayscanner or iphone
#                     --name=statue_2 --data.scene=statue
#                     --data.val_sub= --resume
#
def main():

    log.process(os.getpid())
    log.title("[{}] (PyTorch code for evaluating NeRF/BARF)".format(sys.argv[0]))

    opt_cmd = options.parse_arguments(sys.argv[1:])
    opt = options.set(opt_cmd=opt_cmd)

    with torch.cuda.device(opt.device):
        model = importlib.import_module("model.{}".format(opt.model))
        m = model.Model(opt)

        m.load_dataset(opt,eval_split="test")
        m.build_networks(opt)

        if opt.model=="barf":

            # m.generate_optim_pose_oneNall(opt) # 논문에 그릴 포즈 select 하기 위한 파트
            # m.generate_optim_pose_onebyone(opt)  # 논문에 그릴 포즈 select 하기 위한 파트
            # m.generate_optim_pose(opt) #  논문에 넣을 select한 포즈만 그리기 위한 파트
            m.generate_videos_pose(opt)
            m.restore_checkpoint(opt)
            m.evaluate_ckt(opt)


        m.restore_checkpoint(opt)
        if opt.data.dataset in ["blender","llff","arkit","iphone","strayscanner"]:
            m.evaluate_full(opt)   # test pose novel_view

        # #novel_view synthesis
        m.restore_checkpoint(opt)
        m.generate_videos_synthesis(opt) # load test pose novel_view   : make novel_view
        m.generate_videos_synthesis_origin(opt) # origin novel view  : make novel_view_origin
        """
            origin_novel_view : train 과정에서 optimize한 포즈 범위에서 novel_view 생성
            novel_view : GT 포즈 범위에서 novel view 생성
        """

if __name__=="__main__":
    main()
