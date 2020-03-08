import os
wd = '/home/xin/OneDrive/Working_directory/Continuous_cGAN/CellCounting'

os.chdir(wd)
import timeit
import torch
import torchvision
import torchvision.transforms as transforms
import numpy as np
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.nn import functional as F
import random
import matplotlib.pyplot as plt
import matplotlib as mpl
from torch import autograd
from torchvision.utils import save_image
from tqdm import tqdm
import gc
from itertools import groupby
import argparse
import multiprocessing
from multiprocessing import Pool
from scipy.stats import ks_2samp
import h5py
import pickle
from torch.utils.tensorboard import SummaryWriter

from utils import *
from models import *
from Train_DCGAN import *
from Train_cDCGAN import *
from Train_Continuous_cDCGAN import *

#######################################################################################
'''                                   Settings                                      '''
#######################################################################################
parser = argparse.ArgumentParser(description='Density-ratio based sampling for GANs')

parser.add_argument('--Dataset', type=str, default='Cell300',
                    choices=['VGG','Cell300'])
parser.add_argument('--num_imgs_per_count', type=int, default=10, metavar='N',
                    help='number of images for each cell count')
parser.add_argument('--img_size', type=int, default=64, metavar='N',
                    choices=[64,128])
parser.add_argument('--start_count', type=int, default=0, metavar='N')
parser.add_argument('--end_count', type=int, default=300, metavar='N')
parser.add_argument('--stepsize_count', type=int, default=4, metavar='N')

parser.add_argument('--GAN', type=str, default='Continuous_cDCGAN',
                    choices=['DCGAN', 'cDCGAN', 'Continuous_cDCGAN'])
parser.add_argument('--seed', type=int, default=2019, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--transform', action='store_true', default=False,
                    help='rotate or flip images for GAN training')
parser.add_argument('--normalize_count', action='store_true', default=False,
                    help='normalize cell counts')


parser.add_argument('--kernel_sigma', type=float, default=0.1)
parser.add_argument('--threshold_type', type=str, default='soft',
                    choices=['soft', 'hard'])
parser.add_argument('--kappa', type=float, default=1)
parser.add_argument('--b_int_digits', type=int, default=16,
                    help='How many digits used to represent the integer part of a label')
parser.add_argument('--b_dec_digits', type=int, default=16,
                    help='How many digits used to represent the decimal part of a label')


parser.add_argument('--epoch_gan', type=int, default=500)
parser.add_argument('--lr_g_gan', type=float, default=1e-4,
                    help='learning rate for generator')
parser.add_argument('--lr_d_gan', type=float, default=2e-4,
                    help='learning rate for discriminator')
parser.add_argument('--dim_gan', type=int, default=128,
                    help='Latent dimension of GAN')
parser.add_argument('--batch_size_gan', type=int, default=64, metavar='N',
                    help='input batch size for training GAN')
parser.add_argument('--resumeTrain_gan', type=int, default=0)


parser.add_argument('--samp_batch_size', type=int, default=10)
parser.add_argument('--nfake', type=int, default=50000)

args = parser.parse_args()

print("Sigma is {}, Kappa is {}".format(args.kernel_sigma, args.kappa))


#-----------------------------
# images
NC = 1 #number of channels
IMG_SIZE = 64

#--------------------------------
# system
NGPU = torch.cuda.device_count()
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
NCPU = multiprocessing.cpu_count()
cudnn.benchmark = True # For fast training

#-------------------------------
# GAN
ResumeEpoch_gan = args.resumeTrain_gan
ADAM_beta1 = 0.5 #parameters for ADAM optimizer
ADAM_beta2 = 0.999

#-------------------------------
# sampling parameters
samp_batch_size = args.samp_batch_size #batch size for sampling from GAN or enhanced sampler

#-------------------------------
# seeds
random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.deterministic = True
np.random.seed(args.seed)

#-------------------------------
# output folders
save_models_folder = wd + '/Output/saved_models'
os.makedirs(save_models_folder, exist_ok=True)
save_images_folder = wd + '/Output/saved_images'
os.makedirs(save_images_folder, exist_ok=True)
# save_GANimages_InTrain_folder = wd + '/Output/saved_images/' + args.GAN + '_InTrain/'
save_GANimages_InTrain_folder = wd + '/Output/saved_images/' + args.GAN + '_' + args.threshold_type + '_' + str(args.kernel_sigma) + '_' + str(args.kappa) + '_InTrain/'
os.makedirs(save_GANimages_InTrain_folder, exist_ok=True)
# save_GANimages_folder = wd + '/Output/saved_images/' + args.GAN
save_GANimages_folder = wd + '/Output/saved_images/' + args.GAN + '_' + args.threshold_type + '_' + str(args.kernel_sigma) + '_' + str(args.kappa)
os.makedirs(save_GANimages_folder, exist_ok=True)
save_traincurves_folder = wd + '/Output/Training_loss_fig'
os.makedirs(save_traincurves_folder, exist_ok=True)



#######################################################################################
'''                                    Data loader                                 '''
#######################################################################################
# data loader

if args.Dataset=='VGG':
    h5py_file = wd+'/data/VGG_dataset_+64x64.h5'
    hf = h5py.File(h5py_file, 'r')
    counts = hf['CellCounts'][:]
    counts = counts.astype(np.float)
    images = hf['IMGs_grey'][:]
    hf.close()
elif args.Dataset=='Cell300':
    h5py_file = wd+'/data/Cell300_' + str(args.img_size) + 'x' + str(args.img_size) + '.h5'
    hf = h5py.File(h5py_file, 'r')
    counts = hf['CellCounts'][:]
    counts = counts.astype(np.float)
    images = hf['IMGs_grey'][:]
    hf.close()

    # for each cell count select n_imgs_per_cellcount images
    n_imgs_per_cellcount = args.num_imgs_per_count
    # unique_cellcounts = list(set(counts))
    # n_unique_cellcount = len(unique_cellcounts)
    selected_cellcounts = np.arange(args.start_count, args.end_count+1, args.stepsize_count)
    n_unique_cellcount = len(selected_cellcounts)


    images_subset = np.zeros((n_imgs_per_cellcount*n_unique_cellcount, 1, IMG_SIZE, IMG_SIZE), dtype=np.uint8)
    counts_subset = np.zeros(n_imgs_per_cellcount*n_unique_cellcount)
    for i in range(n_unique_cellcount):
        curr_cellcount = selected_cellcounts[i]
        index_curr_cellcount = np.where(counts==curr_cellcount)[0]

        if i == 0:
            images_subset = images[index_curr_cellcount[0:n_imgs_per_cellcount]]
            counts_subset = counts[index_curr_cellcount[0:n_imgs_per_cellcount]]
        else:
            images_subset = np.concatenate((images_subset, images[index_curr_cellcount[0:n_imgs_per_cellcount]]), axis=0)
            counts_subset = np.concatenate((counts_subset, counts[index_curr_cellcount[0:n_imgs_per_cellcount]]))
    # for i
    images = images_subset
    counts = counts_subset
    del images_subset, counts_subset; gc.collect()

print("Number of images: %d" % len(images))

if args.GAN == "cDCGAN": #treated as classification; convert cell counts to class labels
    unique_counts = np.sort(np.array(list(set(counts)))).astype(np.int)
    count2class = dict()
    for i in range(len(unique_counts)):
        count2class[unique_counts[i]]=i
    counts_new = -1*np.ones(len(counts))
    for i in range(len(counts)):
        counts_new[i] = count2class[counts[i]]
    assert np.sum(counts_new<0)==0
    counts = counts_new
    del counts_new; gc.collect()
    unique_counts = np.sort(np.array(list(set(counts)))).astype(np.int) #update unique_counts
else:
    #normalize count
    if args.normalize_count:
        min_count = np.min(counts)
        max_count = np.max(counts)
        if min_count<0:
            shift_count = abs(min_count)
            max_count = max_count + shift_count
        else:
            shift_count = 0

        counts += shift_count
        counts /= max_count # [0,1]

    else:
        shift_count = 0
        max_count = 1




if args.transform:
    trainset = IMGs_dataset(images, counts, normalize=True, rotate=True, degrees = [90,180,270], hflip = True, vflip = True)
else:
    trainset = IMGs_dataset(images, counts, normalize=True)
trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size_gan, shuffle=True, num_workers=8)






#######################################################################################
'''                                    GAN training                                 '''
#######################################################################################
Filename_GAN = save_models_folder + '/ckpt_' + args.GAN + '_epoch_' + str(args.epoch_gan) + '_SEED_' + str(args.seed) + '_' + args.threshold_type + '_' + str(args.kernel_sigma) + '_' + str(args.kappa)

start = timeit.default_timer()
print("\n Begin Training %s:" % args.GAN)
#----------------------------------------------
# DCGAN
if args.GAN == "DCGAN" and not os.path.isfile(Filename_GAN):
    netG = cnn_generator(NGPU, args.dim_gan)
    netD = cnn_discriminator(True, NGPU)
    if args.resumeTrain_gan==0:
        netG.apply(weights_init)
        netD.apply(weights_init)
    criterion = nn.BCELoss()
    optimizerG = torch.optim.Adam(netG.parameters(), lr=args.lr_g_gan, betas=(ADAM_beta1, ADAM_beta2))
    optimizerD = torch.optim.Adam(netD.parameters(), lr=args.lr_d_gan, betas=(ADAM_beta1, ADAM_beta2))

    # Start training
    netG, netD, optimizerG, optimizerD = train_DCGAN(args.epoch_gan, args.dim_gan, trainloader, netG, netD, optimizerG, optimizerD, criterion, save_GANimages_InTrain_folder, save_models_folder = save_models_folder, ResumeEpoch = args.resumeTrain_gan)

    # store model
    torch.save({
        'netG_state_dict': netG.state_dict(),
        'netD_state_dict': netD.state_dict(),
    }, Filename_GAN)

    # # function for sampling from a trained GAN
    # def fn_sampleGAN_no_label(nfake, batch_size):
    #     images = SampDCGAN(netG, GAN_Latent_Length = args.dim_gan, NFAKE = nfake, batch_size = batch_size, device=device)
    #     return images

#----------------------------------------------
# cDCGAN: treated as a classification dataset
elif args.GAN == "cDCGAN"  and not os.path.isfile(Filename_GAN):
    print("There are {} unique cell counts".format(len(unique_counts)))

    netG = cond_cnn_generator(args.dim_gan, num_classes=len(unique_counts))
    netD = cond_cnn_discriminator(True, num_classes=len(unique_counts))
    if args.resumeTrain_gan==0:
        netG.apply(weights_init)
        netD.apply(weights_init)
    netG = nn.DataParallel(netG)
    netD = nn.DataParallel(netD)
    criterion = nn.BCELoss()
    optimizerG = torch.optim.Adam(netG.parameters(), lr=args.lr_g_gan, betas=(ADAM_beta1, ADAM_beta2))
    optimizerD = torch.optim.Adam(netD.parameters(), lr=args.lr_d_gan, betas=(ADAM_beta1, ADAM_beta2))

    # Start training
    netG, netD, optimizerG, optimizerD = train_cDCGAN(unique_counts, args.epoch_gan, args.dim_gan, trainloader, netG, netD, optimizerG, optimizerD, criterion, save_GANimages_InTrain_folder, save_models_folder = save_models_folder, ResumeEpoch = args.resumeTrain_gan)

    # store model
    torch.save({
        'netG_state_dict': netG.state_dict(),
        'netD_state_dict': netD.state_dict(),
    }, Filename_GAN)

    # # function for sampling from a trained GAN
    # def fn_sampleGAN_with_label(nfake, batch_size):
    #     images, cellcounts = SampcDCGAN(netG, GAN_Latent_Length = args.dim_gan, NFAKE = nfake, batch_size = batch_size, normalize_count = args.normalize_count, shift_label = shift_count, max_label = max_count)
    #     return images, cellcounts

#----------------------------------------------
# Concitnuous cDCGAN
elif args.GAN == "Continuous_cDCGAN":
    if not os.path.isfile(Filename_GAN):
        netG = cont_cond_cnn_generator(args.dim_gan)
        netD = cont_cond_cnn_discriminator(True)
        if args.resumeTrain_gan==0:
            netG.apply(weights_init)
            netD.apply(weights_init)
        netG = nn.DataParallel(netG)
        netD = nn.DataParallel(netD)
        optimizerG = torch.optim.Adam(netG.parameters(), lr=args.lr_g_gan, betas=(ADAM_beta1, ADAM_beta2))
        optimizerD = torch.optim.Adam(netD.parameters(), lr=args.lr_d_gan, betas=(ADAM_beta1, ADAM_beta2))

        tfboard_writer = SummaryWriter(wd+'/Output/saved_logs')

        # Start training
        netG, netD, optimizerG, optimizerD = train_Continuous_cDCGAN(counts, args.kernel_sigma, args.threshold_type, args.kappa, args.epoch_gan, args.dim_gan, trainloader, netG, netD, optimizerG, optimizerD, save_GANimages_InTrain_folder, save_models_folder = save_models_folder, ResumeEpoch = args.resumeTrain_gan, tfboard_writer=tfboard_writer)

        # store model
        torch.save({
            'netG_state_dict': netG.state_dict(),
            'netD_state_dict': netD.state_dict(),
        }, Filename_GAN)

        # # function for sampling from a trained GAN
        # def fn_sampleGAN_with_label(nfake, batch_size):
        #     images, cellcounts = SampcDCGAN(netG, GAN_Latent_Length = args.dim_gan, NFAKE = nfake, batch_size = batch_size, normalize_count = args.normalize_count, shift_label = shift_count, max_label = max_count)
        #     return images, cellcounts
    else:
        checkpoint = torch.load(Filename_GAN)
        netG = cond_cnn_generator(args.dim_gan).to(device)
        netG = nn.DataParallel(netG)
        netG.load_state_dict(checkpoint['netG_state_dict'])

    num_unique_counts_output = 50
    output_counts_range = np.linspace(np.min(counts), np.max(counts), num=num_unique_counts_output)

    for i in range(num_unique_counts_output):
        curr_folder = save_GANimages_folder + '/' +  str(output_counts_range[i])
        os.makedirs(curr_folder, exist_ok=True)
        curr_count = output_counts_range[i]
        _ = SampCcGAN_given_label(netG, curr_count, path=curr_folder, dim_GAN = 128, NFAKE = 100, batch_size = 100, device="cuda")





stop = timeit.default_timer()
print("GAN training finished; Time elapses: {}s".format(stop - start))
