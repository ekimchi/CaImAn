#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Complete pipeline for online processing using OnACID.
@author: Andrea Giovannucci @agiovann and Eftychios Pnevmatikakis @epnev
Special thanks to Andreas Tolias and his lab at Baylor College of Medicine
for sharing their data used in this demo.
"""

import os
import sys

#try:
#    here = os.path.dirname(os.path.realpath(__file__))
#    caiman_path = os.path.join(here, "..", "..")
#    print("Caiman path detected as " + caiman_path)
#    sys.path.append(caiman_path)
#except:
#    pass
import numpy as np
try:
    if __IPYTHON__:
        print('Detected iPython')
        # this is used for debugging purposes only. allows to reload classes when changed
        get_ipython().magic('load_ext autoreload')
        get_ipython().magic('autoreload 2')
except NameError:
    pass

from keras import backend as K
import tensorflow as tf
config = tf.ConfigProto(intra_op_parallelism_threads=1,
            inter_op_parallelism_threads=2,
            allow_soft_placement=False,
            device_count = {'CPU': 2})
session = tf.Session(config=config)
K.set_session(session)

from time import time
import caiman as cm
from caiman.utils.visualization import view_patches_bar
from caiman.utils.utils import download_demo, load_object, save_object
import pylab as pl
import scipy
from caiman.motion_correction import motion_correct_iteration_fast
import cv2
from caiman.utils.visualization import plot_contours
import glob
from caiman.source_extraction.cnmf.online_cnmf import bare_initialization
from copy import deepcopy

#%%  download and list all files to be processed

# folder inside ./example_movies where files will be saved
#fld_name = 'Mesoscope'
#download_demo('Tolias_mesoscope_1.hdf5', fld_name)
#download_demo('Tolias_mesoscope_2.hdf5', fld_name)
#download_demo('Tolias_mesoscope_3.hdf5', fld_name)
#
## folder where files are located
#folder_name = os.path.join(caiman_path, 'example_movies', fld_name)
#extension = 'hdf5'                                  # extension of files
#fls = ['example_movies/Mesoscope/Tolias_mesoscope_1.hdf5','example_movies/Mesoscope/Tolias_mesoscope_2.hdf5','example_movies/Mesoscope/Tolias_mesoscope_3.hdf5']
#fls = fls[:]
# read all files to be processed
#%%
ploton = False
#fls = ['/mnt/ceph/neuro/zebra/05292014Fish1-4/images/mmap_tifs/Plane17_100_500_400_-350_mc_noinit_small.tif']
#K = 10
#min_num_trial = 50
#fls = ['/mnt/ceph/neuro/zebra/05292014Fish1-4/images/mmap_tifs/Plane17_100_500_400_-350_mc_noinit.tif']
#K = 100
#min_num_trial = 60
#
fls = ['/mnt/ceph/neuro/zebra/05292014Fish1-4/images/mmap_tifs/Plane17_mc.hdf5']
K = 500
min_num_trial = 100
# your list of files should look something like this
print(fls)
decay_time = 1.5
gSig = (9, 9)
rval_thr = 1

# number of passes over the data
epochs = 2
#%%   Set up some parameters

# frame rate (Hz)
fr = 2
#fr = 15

# approximate length of transient event in seconds

#decay_time = 0.5

# expected half size of neurons
#gSig = (2.5, 2.5)

# order of AR indicator dynamics
p = 1
# minimum SNR for accepting new components
min_SNR = 2.5
# correlation threshold for new component inclusion

#rval_thr = 0.85

# spatial downsampling factor (increases speed but may lose some fine structure)
ds_factor = 2
# number of background components
gnb = 3
# recompute gSig if downsampling is involved
gSig = tuple((np.array(gSig) / ds_factor))#.astype('int'))
# flag for online motion correction
mot_corr = False
# maximum allowed shift during motion correction
max_shift = np.ceil(10. / ds_factor).astype('int')

# set up some additional supporting parameters needed for the algorithm (these are default values but change according to dataset characteristics)

# number of shapes to be updated each time (put this to a finite small value to increase speed)
max_comp_update_shape = np.inf
# number of files used for initialization
init_files = 1
# number of files used for online
online_files = len(fls) - 1
# number of frames for initialization (presumably from the first file)
initbatch = 200
# maximum number of expected components used for memory pre-allocation (exaggerate here)
expected_comps = 600
# initial number of components
# number of timesteps to consider when testing new neuron candidates
N_samples = np.ceil(fr * decay_time)
# exceptionality threshold
thresh_fitness_raw = scipy.special.log_ndtr(-min_SNR) * N_samples

# upper bound for number of frames in each file (used right below)
len_file = 1815#1885
# total length of all files (if not known use a large number, then truncate at the end)
T1 = len(fls) * len_file * epochs

#%%    Initialize movie
# load only the first initbatch frames and possibly downsample them
if ds_factor > 1:
    Y = cm.load(fls[0], subindices=slice(0, initbatch, None)).astype(
        np.float32).resize(1. / ds_factor, 1. / ds_factor)
else:
    Y = cm.load(fls[0], subindices=slice(
        0, initbatch, None)).astype(np.float32)

if mot_corr:                                        # perform motion correction on the first initbatch frames
    mc = Y.motion_correct(max_shift, max_shift)
    Y = mc[0].astype(np.float32)
    borders = np.max(mc[1])
else:
    Y = Y.astype(np.float32)

# minimum value of movie. Subtract it to make the data non-negative
img_min = Y.min()
Y -= img_min
img_norm = np.std(Y, axis=0)
# normalizing factor to equalize the FOV
img_norm += np.median(img_norm)
Y = Y / img_norm[None, :, :]                        # normalize data

_, d1, d2 = Y.shape
dims = (d1, d2)                                     # dimensions of FOV
Yr = Y.to_2D().T                                    # convert data into 2D array

Cn_init = Y.local_correlations(swap_dim=False)    # compute correlation image
if ploton:
    pl.imshow(Cn_init)
    pl.title('Correlation Image on initial batch')
    pl.colorbar()

#%% initialize OnACID with bare initialization
cnm_init = bare_initialization(Y[:initbatch].transpose(1, 2, 0), init_batch=initbatch, k=K, gnb=gnb,
                               gSig=gSig, p=0, minibatch_shape=100, minibatch_suff_stat=5,
                               update_num_comps=True, rval_thr=rval_thr,
                               thresh_fitness_raw=thresh_fitness_raw,
                               batch_update_suff_stat=True, max_comp_update_shape=max_comp_update_shape,
                               deconv_flag=False, use_dense=False,
                               simultaneously=False, n_refit=0)

#%% Plot initialization results
if ploton:
    crd = plot_contours(cnm_init.A.tocsc(), Cn_init, thr=0.9)
    A, C, b, f, YrA, sn = cnm_init.A, cnm_init.C, cnm_init.b, cnm_init.f, cnm_init.YrA, cnm_init.sn
    view_patches_bar(Yr, scipy.sparse.coo_matrix(
        A.tocsc()[:, :]), C[:, :], b, f, dims[0], dims[1], YrA=YrA[:, :], img=Cn_init)


#%% create a function for plotting results in real time if needed
def create_frame(cnm2, img_norm, captions):
    A, b = cnm2.Ab[:, cnm2.gnb:], cnm2.Ab[:, :cnm2.gnb].toarray()
    C, f = cnm2.C_on[cnm2.gnb:cnm2.M, :], cnm2.C_on[:cnm2.gnb, :]
    # inferred activity due to components (no background)
    comps_frame = A.dot(C[:, t - 1]).reshape(cnm2.dims,
                                             order='F') * img_norm / np.max(img_norm)

#    comps_frame = cnm2.sv.reshape(cnm2.dims, order='C')/100


    bgkrnd_frame = b.dot(f[:, t - 1]).reshape(cnm2.dims, order='F') * \
        img_norm / np.max(img_norm)  # denoised frame (components + background)



    if show_residuals:
        all_comps = np.reshape(cnm2.Yres_buf.mean(
            0), cnm2.dims, order='F') * img_norm / np.max(img_norm)
        all_comps = np.minimum(np.maximum(all_comps * 10, 0), 255)
    else:
        all_comps = (np.array(A.sum(-1)).reshape(cnm2.dims, order='F')
                     )                         # spatial shapes


    frame_comp_1 = cv2.resize(np.concatenate([frame_ / np.max(img_norm), all_comps * 3.], axis=-1),
                              (2 * np.int(cnm2.dims[1] * resize_fact), np.int(cnm2.dims[0] * resize_fact)))

    frame_comp_2 = cv2.resize(np.concatenate([comps_frame * 10., comps_frame + bgkrnd_frame],
                                             axis=-1), (2 * np.int(cnm2.dims[1] * resize_fact), np.int(cnm2.dims[0] * resize_fact)))

    frame_pn = np.concatenate([frame_comp_1, frame_comp_2], axis=0).T
    vid_frame = np.repeat(frame_pn[:, :, None], 3, axis=-1)
    vid_frame = np.minimum((vid_frame * 255.), 255).astype('u1')

    if show_residuals and cnm2.ind_new_all:
        add_v = np.int(cnm2.dims[1]*resize_fact)
        for ind_new in cnm2.ind_new_all:
            cv2.rectangle(vid_frame,(int(ind_new[0][1]*resize_fact),int(ind_new[1][1]*resize_fact)+add_v),
                                         (int(ind_new[0][0]*resize_fact),int(ind_new[1][0]*resize_fact)+add_v),(255,255,255),2)
    if show_residuals and cnm2.ind_new:
        add_v = np.int(cnm2.dims[1]*resize_fact)
        for ind_new in cnm2.ind_new:
            cv2.rectangle(vid_frame,(int(ind_new[0][1]*resize_fact),int(ind_new[1][1]*resize_fact)+add_v),
                                         (int(ind_new[0][0]*resize_fact),int(ind_new[1][0]*resize_fact)+add_v),(255,0,255),2)


    cv2.putText(vid_frame, captions[0], (5, 20), fontFace=5, fontScale=1.2, color=(
        0, 255, 0), thickness=1)
    cv2.putText(vid_frame, captions[1], (np.int(
        cnm2.dims[0] * resize_fact) + 5, 20), fontFace=5, fontScale=1.2, color=(0, 255, 0), thickness=1)
    cv2.putText(vid_frame, captions[2], (5, np.int(
        cnm2.dims[1] * resize_fact) + 20), fontFace=5, fontScale=1.2, color=(0, 255, 0), thickness=1)
    cv2.putText(vid_frame, captions[3], (np.int(cnm2.dims[0] * resize_fact) + 5, np.int(
        cnm2.dims[1] * resize_fact) + 20), fontFace=5, fontScale=1.2, color=(0, 255, 0), thickness=1)
    cv2.putText(vid_frame, 'Frame = ' + str(t), (vid_frame.shape[1] // 2 - vid_frame.shape[1] //
                                                 10, vid_frame.shape[0] - 20), fontFace=5, fontScale=1.2, color=(0, 255, 255), thickness=1)
    return vid_frame

#%% Prepare object for OnACID
cnm2 = deepcopy(cnm_init)
path_to_model = '/mnt/home/agiovann/SOFTWARE/CaImAn/use_cases/edge-cutter/residual_classifier_2classes.h5'
path_to_model = '/mnt/home/agiovann/SOFTWARE/CaImAn/use_cases/CaImAnpaper/net_models/sniper_sensitive.h5'
save_init = False     # flag for saving initialization object. Useful if you want to check OnACID with different parameters but same initialization
if save_init:
    cnm_init.dview = None
    save_object(cnm_init, fls[0][:-4] + '_DS_' + str(ds_factor) + '.pkl')
    cnm_init = load_object(fls[0][:-4] + '_DS_' + str(ds_factor) + '.pkl')

cnm2._prepare_object(np.asarray(Yr), T1, expected_comps, idx_components=None,
                         min_num_trial=min_num_trial, max_num_added = min_num_trial, N_samples_exceptionality=int(N_samples),
                         path_to_model = path_to_model,
                         sniper_mode = True)
cnm2.thresh_CNN_noisy = 0.75
#%% Run OnACID and optionally plot results in real time
cnm2.Ab_epoch = []                       # save the shapes at the end of each epoch
t = cnm2.initbatch                       # current timestep
tottime = []
Cn = Cn_init.copy()
cnn_pos = []
# flag for plotting contours of detected components at the end of each file
plot_contours_flag = False
# flag for showing video with results online (turn off flags for improving speed)
play_reconstr = False
# flag for saving movie (file could be quite large..)
save_movie = False
if save_movie:
    movie_name = os.path.join(folder_name, 'output.avi')  # name of movie to be saved
resize_fact = 1.2                        # image resizing factor

if online_files == 0:                    # check whether there are any additional files
    process_files = fls[:init_files]     # end processing at this file
    init_batc_iter = [initbatch]         # place where to start
    end_batch = T1
else:
    process_files = fls[:init_files + online_files]     # additional files
    # where to start reading at each file
    init_batc_iter = [initbatch] + [0] * online_files


shifts = []
show_residuals = True
if show_residuals:
    caption = 'Mean Residual Bufer'
else:
    caption = 'Identified Components'
captions = ['Raw Data', 'Inferred Activity', caption, 'Denoised Data']
if save_movie and play_reconstr:
    #fourcc = cv2.VideoWriter_fourcc('8', 'B', 'P', 'S')
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out = cv2.VideoWriter(movie_name, fourcc, 30.0, tuple(
        [int(2 * x * resize_fact) for x in cnm2.dims]))

for iter in range(epochs):
    if iter > 0:
        # if not on first epoch process all files from scratch
        process_files = fls[:init_files + online_files]
        init_batc_iter = [0] * (online_files + init_files)

    # np.array(fls)[np.array([1,2,3,4,5,-5,-4,-3,-2,-1])]:
    for file_count, ffll in enumerate(process_files):
        print('Now processing file ' + ffll)
        Y_ = cm.load(ffll, subindices=slice(
            init_batc_iter[file_count], T1, None))

        # update max-correlation (and perform offline motion correction) just for illustration purposes
        if plot_contours_flag:
            if ds_factor > 1:
                Y_1 = Y_.resize(1. / ds_factor, 1. / ds_factor, 1)
            else:
                Y_1 = Y_.copy()
            if mot_corr:
                templ = (cnm2.Ab.data[:cnm2.Ab.indptr[1]] * cnm2.C_on[0,
                                                                      t - 1]).reshape(cnm2.dims, order='F') * img_norm
                newcn = (Y_1 - img_min).motion_correct(max_shift, max_shift,
                                                       template=templ)[0].local_correlations(swap_dim=False)
                Cn = np.maximum(Cn, newcn)
            else:
                Cn = np.maximum(Cn, Y_1.local_correlations(swap_dim=False))

        old_comps = cnm2.N                              # number of existing components
        for frame_count, frame in enumerate(Y_):        # now process each file
            if np.isnan(np.sum(frame)):
                raise Exception('Frame ' + str(frame_count) + ' contains nan')
            if t % 100 == 0:
                print('Epoch: ' + str(iter + 1) + '. ' + str(t) + ' frames have beeen processed in total. ' + str(cnm2.N -
                                                                                                                  old_comps) + ' new components were added. Total number of components is ' + str(cnm2.Ab.shape[-1] - gnb))
                old_comps = cnm2.N

            t1 = time()                                 # count time only for the processing part
            frame_ = frame.copy().astype(np.float32)    #
            if ds_factor > 1:
                frame_ = cv2.resize(
                    frame_, img_norm.shape[::-1])   # downsampling

            frame_ -= img_min                                       # make data non-negative

            if mot_corr:                                            # motion correct
                templ = cnm2.Ab.dot(
                    cnm2.C_on[:cnm2.M, t - 1]).reshape(cnm2.dims, order='F') * img_norm
                frame_cor, shift = motion_correct_iteration_fast(
                    frame_, templ, max_shift, max_shift)
                shifts.append(shift)
            else:
                templ = None
                frame_cor = frame_

            frame_cor = frame_cor / img_norm                        # normalize data-frame
            cnm2.fit_next(t, frame_cor.reshape(-1, order='F')
                          )      # run OnACID on this frame
            # store time
            cnn_pos.append(cnm2.cnn_pos)
            tottime.append(time() - t1)

            t += 1

            if t % 1000 == 0 and plot_contours_flag:
                pl.cla()
                A = cnm2.Ab[:, cnm2.gnb:]
                # update the contour plot every 1000 frames
                crd = cm.utils.visualization.plot_contours(A, Cn, thr=0.9)
                pl.pause(1)

            if play_reconstr:                                               # generate movie with the results
                vid_frame = create_frame(cnm2, img_norm, captions)
                if save_movie:
                    out.write(vid_frame)
                cv2.imshow('frame', vid_frame)
                if cv2.waitKey(100) & 0xFF == ord('q'):
                    break

        print('Cumulative processing speed is ' + str((t - initbatch) /
                                                      np.sum(tottime))[:5] + ' frames per second.')
    # save the shapes at the end of each epoch
    cnm2.Ab_epoch.append(cnm2.Ab.copy())

if save_movie:
    out.release()
cv2.destroyAllWindows()
#%%  save results (optional)
save_results = True

if save_results:
    np.savez('results_analysis_online_Plane17_mc_new_efty.npz',
             Cn=Cn, Ab=cnm2.Ab, Cf=cnm2.C_on, b=cnm2.b, f=cnm2.f,
             dims=cnm2.dims, tottime=tottime, noisyC=cnm2.noisyC, shifts=shifts)
#%%
if ploton:

    m = cm.load(fls)
    mp = (m.computeDFF(3))
    Cn = cv2.resize(mp[0].local_correlations(eight_neighbours=True, swap_dim=False),dims[::-1])
#%% extract results from the objects and do some plotting
if ploton:
    A, b = cnm2.Ab[:, cnm2.gnb:], cnm2.Ab[:, :cnm2.gnb].toarray()
    C, f = cnm2.C_on[cnm2.gnb:cnm2.M, t - t //
                     epochs:t], cnm2.C_on[:cnm2.gnb, t - t // epochs:t]
    noisyC = cnm2.noisyC[:, t - t // epochs:t]
    b_trace = [osi.b for osi in cnm2.OASISinstances] if hasattr(
        cnm2, 'OASISinstances') else [0] * C.shape[0]

    pl.figure()
    crd = cm.utils.visualization.plot_contours(A, Cn, thr=0.9, vmax = 0.5)
    view_patches_bar(Yr, scipy.sparse.coo_matrix(A.tocsc()[:, :]), C[:, :], b, f,
                     dims[0], dims[1], YrA=noisyC[cnm2.gnb:cnm2.M] - C, img=Cn)

#%%
#df_f = detrend_df_f_auto(A,b,C,f,YrA)