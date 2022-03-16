import pickle
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
import os
import numpy as np
import imageio
import json
import cv2
from transforms3d.quaternions import quat2mat
from skimage import img_as_ubyte
np.random.seed(0)

# python run_nerf.py --expname computer
def config_parser():
    import configargparse
    parser = configargparse.ArgumentParser()
    parser.add_argument("--expname", type=str,
                        help='experiment name')
    parser.add_argument("--basedir", type=str, default='./arkit/',
                        help='input data directory')

    #keyframe options
    parser.add_argument("--min_angle_keyframe", type=float, default=2,
                        help='minimum angle between key frames')
    parser.add_argument("--min_distance_keyframe", type=float, default=0.01,
                        help='minimum distance between key frames')
    return parser

def rotx(t):
    ''' 3D Rotation about the x-axis. '''
    c = np.cos(t)
    s = np.sin(t)
    return np.array([[1, 0, 0],
                     [0, c, -s],
                     [0, s, c]])

def extract_frames(video_path, out_folder, size):
    origin_size=[]
    """mp4 to image frame"""
    cap = cv2.VideoCapture(video_path)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    for i in tqdm(range(frame_count)):
        ret, frame = cap.read()
        if ret is not True:
            break
        frame = cv2.resize(frame, size) #이미지 사이즈 변경에 따른 instrinsic 변화는 아래에 있음
        cv2.imwrite(os.path.join(out_folder, str(i).zfill(5) + '.jpg'), frame)
    return origin_size

#SyncedPose.txt 만드는
def sync_intrinsics_and_poses(cam_file, pose_file, out_file):
    """Load camera intrinsics"""  # frane.txt -> camera intrinsics
    assert os.path.isfile(cam_file), "camera info:{} not found".format(cam_file)
    with open(cam_file, "r") as f:  # frame.txt 읽어서
        cam_intrinsic_lines = f.readlines()

    cam_intrinsics = []
    for line in cam_intrinsic_lines:
        line_data_list = line.split(',')
        if len(line_data_list) == 0:
            continue
        cam_intrinsics.append([float(i) for i in line_data_list])
        # frame.txt -> cam_instrinsic
    K = np.array([
            [cam_intrinsics[0][2], 0, cam_intrinsics[0][4]],
            [0, cam_intrinsics[0][3], cam_intrinsics[0][5]],
            [0, 0, 1]
        ])

    """load camera poses"""  # ARPose.txt -> camera pose  gt
    assert os.path.isfile(pose_file), "camera info:{} not found".format(pose_file)
    with open(pose_file, "r") as f:
        cam_pose_lines = f.readlines()

    cam_poses = []
    for line in cam_pose_lines:
        line_data_list = line.split(',')
        if len(line_data_list) == 0:
            continue
        cam_poses.append([float(i) for i in line_data_list])

    """ outputfile로 syncpose 맞춰서 내보냄  """
    lines = []
    ip = 0
    length = len(cam_poses)

    for i in range(len(cam_intrinsics)):
        while ip + 1 < length and abs(cam_poses[ip + 1][0] - cam_intrinsics[i][0]) < abs(
                cam_poses[ip][0] - cam_intrinsics[i][0]):
            ip += 1
        cam_pose = cam_poses[ip][:4] + cam_poses[ip][5:] + [cam_poses[ip][4]]
        line = [str(a) for a in cam_pose] #time,tx,ty,tz,qw,qx,qy,qz #TODO : timestamp.....
        line[0] = str(i).zfill(5)  # name,tx,ty,tz,qw,qx,qy,qz
        lines.append(' '.join(line) + '\n')

    dirname = os.path.dirname(out_file)
    if not os.path.exists(dirname):
        os.makedirs(dirname)

    with open(out_file, 'w') as f:
        f.writelines(lines)
    return K

def load_camera_pose(cam_pose_dir): # SyncedPose.txt
    if cam_pose_dir is not None and os.path.isfile(cam_pose_dir):
        pass
    else:
        raise FileNotFoundError("Given camera pose dir:{} not found"
                                .format(cam_pose_dir))

    pose = []
    def process(line_data_list):   #syncedpose.txt  : imagenum(string) tx ty tz(m) qx qy qz qw
        line_data = np.array(line_data_list, dtype=float)
        # fid = line_data_list[0] #0부터
        trans = line_data[1:4]
        quat = line_data[4:]
        rot_mat = quat2mat(np.append(quat[-1], quat[:3]).tolist())
                            # 여기선 (w,x,y,z) 순 인듯
        #TODO :check
        rot_mat = rot_mat.dot(np.array([  #axis flip..?
            [1, 0, 0],
            [0, -1, 0],
            [0, 0, -1]
        ]))
        rot_mat = rotx(np.pi / 2) @ rot_mat #3D Rotation about the x-axis.
        trans = rotx(np.pi / 2) @ trans
        trans_mat = np.zeros([3, 4])
        trans_mat[:3, :3] = rot_mat
        trans_mat[:3, 3] = trans
        trans_mat = np.vstack((trans_mat, [0, 0, 0, 1]))
        pose.append(trans_mat)

    with open(cam_pose_dir, "r") as f:
        cam_pose_lines = f.readlines()
    for cam_line in cam_pose_lines:
        line_data_list = cam_line.split(" ")
        if len(line_data_list) == 0:
            continue
        process(line_data_list)

    return pose


def process_arkit_data(args,ori_size=(1920, 1440), size=(640, 480)):
    # print("!! ",os.path.realpath(os.path.join(args.basedir)))
    # print("!! ",os.path.abspath(os.path.join(args.basedir)))
    basedir = os.path.join(args.basedir,args.expname)
    # print("!! ",os.path.realpath(basedir))
    # print("!! ",os.path.abspath(basedir))
    print('Extract images from video...')
    video_path = os.path.join(basedir, 'Frames.m4v')
    image_path = os.path.join(basedir, 'images')
    if not os.path.exists(image_path):
        os.mkdir(image_path)
        extract_frames(video_path, out_folder=image_path, size=size) #조건문 안으로 넣음

    # make SyncedPose
    print('Load intrinsics and extrinsics')
    K = sync_intrinsics_and_poses(os.path.join(basedir, 'Frames.txt'), os.path.join(basedir, 'ARposes.txt'),
                            os.path.join(basedir, 'SyncedPoses.txt')) #imagenum(string) tx ty tz(m) qx qy qz qw
    K[0,:] /= (ori_size[0] / size[0]) #TODO: 이거 메인 트레인 파트에서도 해줘야...
    K[1, :] /= (ori_size[1] / size[1])  #resize 전 크기가 orgin_size 이기 때문에

    #quat -> rot
    all_cam_pose = load_camera_pose(os.path.join(basedir, 'SyncedPoses.txt'))

    """Keyframes selection"""
    all_ids = [0]
    last_pose = all_cam_pose[0]
    for i in range(len(all_cam_pose)):
        cam_intrinsic = K
        cam_pose = all_cam_pose[i]
        # translation->0.1m,rotation->15도 max 값 기준 넘는 것만 select
        angle = np.arccos(
            ((np.linalg.inv(cam_pose[:3, :3]) @ last_pose[:3, :3] @ np.array([0, 0, 1]).T) * np.array(
                [0, 0, 1])).sum())
        # extrinsice rotation 뽑아 inverse @  그 전 pose rotation @
        # rotation 사이 연산 후 accose 으로 각 알아내는
        dis = np.linalg.norm(cam_pose[:3, 3] - last_pose[:3, 3])
        # 기준값
        if angle > (args.min_angle_keyframe / 180) * np.pi or dis > args.min_distance_keyframe:
            all_ids.append(i)
            last_pose = cam_pose

    """final select image,poses"""
    imgs = []
    poses = []
    for i in all_ids:
        image_file_name = os.path.join(image_path, str(i).zfill(5) + '.jpg')
        imgs.append(imageio.imread(image_file_name))
        poses.append(all_cam_pose[i])
    imgs = (np.array(imgs) / 255.).astype(np.float32)
    poses = np.array(poses).astype(np.float32)

    """train, val, test"""
    i_split = []
    n = poses.shape[0]  # count of image
    train_indexs = np.linspace(0, n, (int)(n * 0.9), endpoint=False, dtype=int)
    i_split.append(train_indexs)
    val_indexs = np.linspace(0, n, (int)(n * 0.2), endpoint=False, dtype=int)
    i_split.append(val_indexs)
    test_indexs = np.random.choice(n, (int)(n * 0.2))
    i_split.append(test_indexs)
    print('train : {0} , val : {1} , test : {2}'.format(train_indexs.shape[0],val_indexs.shape[0],test_indexs.shape[0]))


    # select 된 pose,image 파일 저장
    def save_keyframe_data(dir, opt='train', index=[] ,images=[], pose=[]):
        image_dir = os.path.join(dir, opt)
        pose_file = os.path.join(dir, 'transforms_{}.txt'.format(opt))
        if not os.path.exists(image_dir):
            os.mkdir(image_dir)

        lines = []
        for i in range(len(index)):
            line = []
            imageio.imwrite('{}/{}.png'.format(image_dir, str(index[i]).zfill(5)), img_as_ubyte(images[i]))
            for j in range(3):
                for k in range(4) :
                    line.append(str(pose[i][j][k]))
            #line =np.concatenate((pose[i][0,:] ,pose[i][1,:] , pose[i][2,:]) , axis=0  )        #pose[i][0,:3] + pose[i][1,:3] + pose[i][2,:3] \
            lines.append(' '.join(line) + '\n') # (3x4)shape이 row 한줄로 이어 붙임.
        with open(pose_file, 'w') as f:
            f.writelines(lines)

    save_keyframe_data(basedir,'train',
                       train_indexs,
                       imgs[train_indexs],
                       poses[train_indexs]);

    save_keyframe_data(basedir,'val',
                       val_indexs,
                       imgs[val_indexs],
                       poses[val_indexs]);

    save_keyframe_data(basedir,'test',
                       test_indexs,
                       imgs[test_indexs],
                       poses[test_indexs]);

# cd data 한 다음에 이 코드 실행해야하나봐 경로 이상해
# python process_arkit_data.py --expname computer
# 다 실행한 이후엔 cd ../ 해주고
if __name__ == '__main__':
    parser = config_parser()
    args = parser.parse_args()
    process_arkit_data(args)