# Ignore warning
import warnings
warnings.filterwarnings(action='ignore')

import os
import json
import pickle
import h5py
from pathlib import Path
from abc import ABC
from glob import glob
import io

import numpy as np
import pandas as pd

import torch
import torch.multiprocessing
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.dataloader import default_collate
from torchvision.transforms import Compose, Lambda, ToTensor
from torchvision.transforms._transforms_video import NormalizeVideo, RandomHorizontalFlipVideo, CenterCropVideo
from pytorchvideo.transforms import ShortSideScale

from PIL import Image

import argparse
import colorful
from tqdm import tqdm

# For LanguageBind
from languagebind import LanguageBind, to_device, transform_dict, LanguageBindImageTokenizer
from pprint import pprint

OPENAI_DATASET_MEAN = (0.48145466, 0.4578275,  0.40821073)
OPENAI_DATASET_STD  = (0.26862954, 0.26130258, 0.27577711)

LOAD_DIR = {
    'ai2'    : '/net/nfs3.prior/dongjook/',
    'align'  : '/gallery_tate/dongyeon.woo/howto100m/howtoalign',}

# utils ----------------------------------------------------------

def get_LB_model(args):
    clip_type = {'video': 'LanguageBind_Video_FT',}  # also LanguageBind_Video
    # model     = LanguageBind(clip_type=clip_type, cache_dir='./cache_dir')
    cache_dir = '/net/nfs3.prior/dongjook/Language_Bind_cache'
    model     = LanguageBind(clip_type=clip_type, cache_dir=cache_dir)
    model     = model.to(args.device)
    pretrained_ckpt = f'LanguageBind/LanguageBind_Image'
    tokenizer = LanguageBindImageTokenizer.from_pretrained(pretrained_ckpt, cache_dir=f'{cache_dir}/tokenizer_cache_dir')
    transform = Compose(
            [
                # UniformTemporalSubsample(num_frames),
                Lambda(lambda x: x / 255.0),
                NormalizeVideo(mean=OPENAI_DATASET_MEAN, std=OPENAI_DATASET_STD),
                ShortSideScale(size=224),
                CenterCropVideo(224),
                RandomHorizontalFlipVideo(p=0.5),
            ])
    model.eval()

    return model, transform, tokenizer

def get_total_video_dict(args):
    if args.dir_name == 'ai2':
        # e.g., /home/dongjook/data/nfs/730k/sentencified_htm_730k_part{4-9}}.json
        # e.g., /home/dongjook/data/nfs/subset/sentencified_htm_subset_part{0-3}.json
        file_name = f'sentencified_htm_{args.data_version}_part{args.meta_part}.json'
        with open(os.path.join(args.root_path, file_name), 'r') as f:
            total_video_dict = json.load(f)

    elif args.dir_name == 'align':
        with open(os.path.join(LOAD_DIR[args.dir_name], 'htm_align_reformatted.json'), 'r') as f:
            total_video_dict = json.load(f)

    elif args.data_version == 'subset':
        with open(os.path.join('/gallery_tate/dongyeon.woo/howto10m/sentencified_htm_subset.json'), 'r') as f:
            total_video_dict = json.load(f)

    else:
        root_path = '/gallery_tate/dongyeon.woo/howto100m/sentencified/'
        file_name = f'sentencified_htm_{args.data_version}_part{args.meta_part}.json'
        with open(os.path.join(root_path, file_name), 'r') as f:
            total_video_dict = json.load(f)

    return total_video_dict


def get_partitioned_dict(args, total_video_dict):
    if args.total==1:
        return total_video_dict

    if args.total > 1:
        total_video_list  = list(total_video_dict.keys())
        part, total       = args.part,  args.total
        total_size        = len(total_video_dict)
        part_size         = int(total_size / total)

        start             = part_size * (part - 1)
        end               = part_size * (part) if part < total else total_size

        new_total_video_dict = {}
        total_video_list = total_video_list[start:end]
        for video_id in total_video_list:
            new_total_video_dict[video_id] = total_video_dict[video_id]
        print(f'[PARTITION] Total datasets  : {len(total_video_dict)}, Part: {part}/{total} [{start}:{end}]')
        print(f'[PARTITION] After partition : {len(new_total_video_dict)}')

    return new_total_video_dict


def get_preprocessed_frames_hdf5(args):
    if args.frame_load == 'hdf5':
        if args.dir_name == 'ai2':
            if args.data_version == '370k':
                hdf5_file = h5py.File(os.path.join(args.root_path, f'preprocessed_frames_total.h5py'),'r')
            elif args.data_version in ['730k', 'valid']:
                hdf5_file = h5py.File(os.path.join(args.root_path, f'preprocessed_frames_part{args.meta_part}.h5py'),'r')

        elif args.dir_name == 'align':
            hdf5_file = h5py.File(os.path.join(LOAD_DIR[args.dir_name], f'preprocessed_frames.h5py'),'r')
            args.use_hdf5_text_emb = False
        else:
            hdf5_file = h5py.File(os.path.join(args.root_path, f'preprocessed_frames_part{args.meta_part}.h5py'),'r')
            # hdf5_real_text_emb_file = h5py.File(os.path.join(LOAD_DIR[args.dir_name], f'real_text_emb_{args.dir_name}_{args.total}_{args.part}.hdf5'),'r')
    else:
        NotImplementedError

    return hdf5_file


def get_h5py_files(args):
    h5py_f = {}
    h5py_f['clip_sim_f'] = h5py.File(os.path.join(args.save_path, f'clip_sim_part{args.meta_part}_{args.total}_{args.part}.hdf5'), 'a')

    for key in h5py_f:
        h5py_f[key].flush()
        if args.dir_name == 'ai2':
            os.chmod(h5py_f[key].filename, mode=0o777)

    return h5py_f

# ----------------------------------------------------------------

# ================
# Generic Datasets
# ================
class BaseDataset(Dataset, ABC):
    name = 'base'
    dataset_size = 0

    def __init__(self):
        super().__init__()

    def __len__(self):
        return self.dataset_size

    def collate_fn(self, batch):
        return default_collate(batch)


# ================
# HowTo100M  Datasets
# ================
class HowTo100M(BaseDataset):
    name = 'howto100m'
    def __init__(self, args, tokenizer, processor, valid_current_video_ids, hdf5_file=None, video_dict=None):
        super(HowTo100M, self).__init__()

        self.args              = args
        self.device            = args.device
        self.feature_extractor = args.feature_extractor

        self.debug        = args.debug
        self.data_version = args.data_version

        self.max_frames = args.max_frames
        self.frame_idxs = [0,1,2,3,4,5,6,7] if self.max_frames==8 else [0,2,4,6] # UMT case
        self.max_words  = 77

        self.hdf5_file = hdf5_file

        # Set preprocess
        self.tokenizer = tokenizer
        self.processor = processor

        self.video_dict = video_dict
        self.valid_current_video_ids = valid_current_video_ids


        # Set dataframe
        df = pd.DataFrame(columns = ['video_id', 'text_id'])
        for video_id in tqdm(valid_current_video_ids):
            max_text_idx       = len(self.video_dict[video_id]['text'])
            cur_df             = pd.DataFrame(columns = ['video_id', 'text_id'])
            cur_df['text_id']  = np.arange(0, max_text_idx)
            cur_df['video_id'] = video_id
            df = pd.concat([df, cur_df], axis=0, ignore_index=True)
        self.df = df


    def _get_frames(self, video_id=None, text_id=None):
        """ Get video information
        INPUT:
            video_id: video_id
            text_id : text_id
        OUTPUT:
            clip_images: (3, max_frame, 224, 224), torch.tensor
                - max_frame : max frame per clip
        """
        #====================
        # Get all frames
        #====================
        if self.args.frame_load == 'hdf5':
            try:
                # Load from frame -----------------------------------------
                binary_images = self.hdf5_file[video_id][text_id*8: (text_id+1)*8][self.frame_idxs]
                images = torch.zeros((self.max_frames, 224, 224, 3))

                for i, binary_image in enumerate(binary_images):
                    images[i] = torch.from_numpy(np.array(Image.open(io.BytesIO(binary_image))))
                # Checking code!
                # Image.open(io.BytesIO(images[0])).save(f'temp/temp.jpg')

                images = images.permute(3, 0, 1, 2) # (T, H, W, C) -> (C, T, H, W)
                images = self.processor(images)
                # ----------------------------------------------------------

            except Exception as e:
                print(f'video_id {video_id}, text_id {text_id} sample is corrupted, {e}')
                return torch.zeros((3, self.max_frames, 224, 224), dtype=torch.float), False # bad clip-captions
        else: 
            NotImplementedError

        return images, True


    def __repr__(self):
        return str(self)


    def __len__(self):
        return len(self.df)


    def __getitem__(self, idx):
        """
        """
        data               = self.df.iloc[idx]
        video_id           = data['video_id']
        text_id            = data['text_id']
        raw_text           = self.video_dict[video_id]['text'][text_id]
        frames, valid_flag = self._get_frames(video_id=video_id, text_id=text_id)

        return video_id, text_id, frames, raw_text, valid_flag


def save_embeds_sims_chunk(args, clip_sim_dict, h5py_f):
    # Save as single video id
    for video_id in clip_sim_dict.keys():
        for key in h5py_f: # To overwrite
            if video_id in h5py_f[key].keys():
                del h5py_f[key][video_id]
        h5py_f['clip_sim_f'].create_dataset(video_id, data = clip_sim_dict[video_id])

    # Flush
    for key in h5py_f:
        h5py_f[key].flush()

    # Save flag after flush
    if not args.debug:
        for video_id in clip_sim_dict.keys():
            flag_save_path = os.path.join(args.flag_dir, f'{video_id}')
            Path(flag_save_path).touch()
    else:
        from IPython import embed; embed(colors='neutral')  # XXX DEBUG  # yapf: disable

    return


# %%
def parse_args():
    parser = argparse.ArgumentParser()
    # For partition
    parser.add_argument("--dir_name", type=str, default='moma', help="[moma, tate, getty, orsay]")
    parser.add_argument("--meta_part", type=int, default=1, help="for mulit_running. which part?(1, ..., total)")
    parser.add_argument("--part", type=int, default=1, help="for mulit_running. which part?(1, ..., total)")
    parser.add_argument("--total", type=int, default=1, help="for multi_running. how many parts?")

    # For dataloader 
    parser.add_argument("--device", type=str, default='cuda')
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--num_workers", type=int, default=4)

    # others
    parser.add_argument("--data_version", type=str, default='730k', help="[subset, 1200k, 730k, 370k, valid]")
    parser.add_argument("--feature_extractor", type=str, default='cv2_frames', help="[cv2_video, cv2_frames]")
    parser.add_argument("--model_id", type=str, default='instructblip', help="[blip2, instructblip]")
    parser.add_argument("--num_segment", type=int, default=200)
    parser.add_argument("--flush", type=int, default=200)

    parser.add_argument("--debug", type=str, default='False')
    parser.add_argument("--frame_load", type=str, default='hdf5', help='[image, hdf5]')
    parser.add_argument("--max_frames", type=int, default=8, help='[4,  8, 12]')

    return parser.parse_args()

# %%
def main(args):
    args.debug = True if args.debug == 'True' else False
    args.root_path = os.path.join(LOAD_DIR[args.dir_name], args.data_version)
    args.save_path = args.root_path if not args.debug else os.path.join(args.root_path, 'debug')
    pprint(args)
    print(f'Debug mode: {args.debug}')

    print('Load model')
    model, transform, tokenizer = get_LB_model(args)

    print('Load json(total_video_dict)')
    total_video_dict = get_total_video_dict(args)
    print(f'Load json(total_video_dict) results: Number of videos = {len(total_video_dict)}')

    print(f'[PARTITION] Select data {args.part}/{args.total}')
    total_video_dict = get_partitioned_dict(args, total_video_dict)
    total_video_ids = list(total_video_dict.keys())

    print(f'Set save path on {args.save_path}')
    args.flag_dir   = os.path.join(args.save_path, 'final_flag')
    os.makedirs(args.flag_dir, exist_ok=True, mode=0o777)
    h5py_f = get_h5py_files(args)

    print(f'Load preprocessed_frames')
    hdf5_file = get_preprocessed_frames_hdf5(args)

    for i in range(0, len(total_video_ids), args.num_segment):
        # Check current video ids and check its validity
        start = i
        end   = (i + args.num_segment) if (i + args.num_segment) < len(total_video_ids) else len(total_video_ids)
        current_video_ids = total_video_ids[start:end]

        # video validitiy check --------------------------------------------
        print(f'[Current_dataset] Validity check on video_id[{start}:{end}]')
        valid_current_video_ids = []
        for current_video_id in tqdm(current_video_ids):
            current_video_id_process_flag = os.path.exists(os.path.join(args.root_path, 'preprocessed_flag', current_video_id))
            # current_video_id_done_flag    = os.path.exists(os.path.join(args.root_path, 'final_flag',        current_video_id))
            current_video_id_done_flag    = (current_video_id in list(h5py_f['clip_sim'].keys()))
            if not args.debug:
                if current_video_id_process_flag & (not current_video_id_done_flag):
                    valid_current_video_ids.append(current_video_id)
            else:
                if current_video_id_process_flag:
                    valid_current_video_ids.append(current_video_id)
        print(f'[Current_dataset] Validity check result: {len(valid_current_video_ids)}/{len(current_video_ids)}')

        if len(valid_current_video_ids) == 0:
            print(f'[Current_dataset] Pass current dataset!')
            continue
        # ------------------------------------------------------------------

        dataset  = HowTo100M(args                   = args,
                            valid_current_video_ids = valid_current_video_ids,
                            tokenizer               = tokenizer,
                            processor               = transform,
                            video_dict              = total_video_dict,
                            hdf5_file               = hdf5_file,)

        dataloader = DataLoader(
            dataset,
            batch_size  = args.batch_size,
            num_workers = args.num_workers,
            # collate_fn  = dataset.collate_fn,
            drop_last   = False,
            pin_memory  = True,  # better when training on GPU.
            shuffle     = False) # Don't need to shuffle for captioning

        print('Start batch')
        clip_sim_dict = {}
        step = 0
        with torch.no_grad():
            pbar = tqdm(dataloader)
            for batch in pbar:
                pbar.set_description(f"[{args.part:2d}/{args.total:2d}] [{start}:{end}({len(valid_current_video_ids)}) in {len(total_video_ids)} ({100*end/len(total_video_ids):.2f}%)] [#clips: {len(dataset)}]")
                video_ids, text_ids, frames, raw_texts, valid_flag = batch
                if np.sum(np.array(valid_flag)) == 0:
                    continue
                video_ids = np.array(video_ids)[np.array(valid_flag)]
                raw_texts = list(np.array(raw_texts)[np.array(valid_flag)])
                text_ids  = text_ids[valid_flag]
                texts     = dataset.tokenizer(raw_texts,
                                        max_length=dataset.max_words,
                                        padding='max_length',
                                        truncation=True,
                                        return_tensors='pt')

                frames                  = frames[valid_flag].to(args.device)
                texts['input_ids']      = texts['input_ids'].to(args.device)
                texts['attention_mask'] = texts['attention_mask'].to(args.device)

                inputs = {'video': {}}
                inputs['video']['pixel_values'] = frames
                inputs['language']              = texts

                embeddings = model(inputs)
                similarity = (embeddings['video'] @ embeddings['language'].T).detach().cpu().numpy()
                similarity = np.diagonal(similarity)
                for v_id, clip_sim in zip(video_ids, similarity):
                    if v_id not in clip_sim_dict.keys():
                        clip_sim_dict[v_id] = clip_sim
                    else:
                        clip_sim_dict[v_id] = np.vstack([clip_sim_dict[v_id], clip_sim])

                step += 1

            save_embeds_sims_chunk(args, clip_sim_dict, h5py_f,)


if __name__ == "__main__":
    args = parse_args()
    main(args)
    print(colorful.bold_pink("Thank you and Good Job Computer.").styled_string)
