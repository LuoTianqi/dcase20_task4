# -*- coding: utf-8 -*-
#########################################################################
# Initial software
# Copyright Nicolas Turpault, Romain Serizel, Justin Salamon, Ankit Parag Shah, 2020, v1.0
# This software is distributed under the terms of the License MIT
#########################################################################

from __future__ import print_function

import glob
import warnings

import numpy as np
import os
import os.path as osp
import librosa
import time
import pandas as pd

import config as cfg
from desed.download_real import download
from utilities.Logger import create_logger
from utilities.utils import read_audio, meta_path_to_audio_dir

logger = create_logger(__name__, terminal_level=cfg.terminal_level)


class DESED:
    """DCASE 2020 task 4 dataset, uses DESED dataset
    Data are organized in `audio/` and corresponding `metadata/` folders.
    audio folder contains wav files, and metadata folder contains .tsv files.

    The organisation should always be the same in the audio and metadata folders. (See example)
    If there are multiple metadata files for a single audio files, add the name in the list of `merged_folders_name`.
    (See validation folder example). Be careful, it works only for one level of folder.

    tab separated value metadata files (.tsv) contains columns:
        - filename                                  (unlabeled data)
        - filename  event_labels                    (weakly labeled data)
        - filename  onset   offset  event_label     (strongly labeled data)

    Example:
    - dataset
        - metadata
            - train
                - weak.tsv              (audio_dir associated: audio/train/weak)
            - validation
                - validation.tsv        (audio_dir associated: audio/validation)
                - test_dcase2018.tsv    (audio_dir associated: audio/validation)
                - eval_dcase2018.tsv    (audio_dir associated: audio/validation)
            -eval
                - public.tsv            (audio_dir associated: audio/eval/public)
        - audio
            - train
                - weak
            - validation
            - eval
                - public
                    - aaa.wav
                    - aaa_sources
                        - foreground
                            - s1.wav
                            - s2.wav
                        - background
                            - s3.wav
                    - bbb.wav

    Args:
        base_feature_dir: str, optional, base directory to store the features
        recompute_features: bool, optional, wether or not to recompute features
        compute_log: bool, optional, whether or not saving the logarithm of the feature or not
            (particularly useful to put False to apply some data augmentation)

    Attributes:
        base_feature_dir: str, base directory to store the features
        recompute_features: bool, wether or not to recompute features
        compute_log: bool, whether or not saving the logarithm of the feature or not
            (particularly useful to put False to apply some data augmentation)
        feature_dir : str, directory to store the features

    """
    def __init__(self, base_feature_dir="features", recompute_features=False, compute_log=True):
        # Parameters, they're kept if we need to reproduce the dataset
        self.sample_rate = cfg.sample_rate
        self.n_window = cfg.n_window
        self.hop_size = cfg.hop_size
        self.n_mels = cfg.n_mels
        self.mel_min_max_freq = (cfg.mel_f_min, cfg.mel_f_max)

        # Defined parameters
        self.recompute_features = recompute_features
        self.compute_log = compute_log

        # Feature dir to not have the same name with different parameters
        ext_freq = ''
        if self.mel_min_max_freq != (0, self.sample_rate / 2):
            ext_freq = f"_{'_'.join(self.mel_min_max_freq)}"
        feature_dir = osp.join(base_feature_dir, f"sr{self.sample_rate}_win{self.n_window}_hop{self.hop_size}"
                                                 f"_mels{self.n_mels}{ext_freq}")
        if not self.compute_log:
            feature_dir += "_nolog"

        self.feature_dir = osp.join(feature_dir, "features")
        self.meta_feat_dir = osp.join(feature_dir, "metadata")
        # create folder if not exist
        os.makedirs(self.feature_dir, exist_ok=True)
        os.makedirs(self.meta_feat_dir, exist_ok=True)

    def state_dict(self):
        """ get the important parameters to save for the class
        Returns:
            dict
        """
        parameters = {
            "feature_dir": self.feature_dir,
            "meta_feat_dir": self.meta_feat_dir,
            "compute_log": self.compute_log,
            "sample_rate": self.sample_rate,
            "n_window": self.n_window,
            "hop_size": self.hop_size,
            "n_mels": self.n_mels,
            "mel_min_max_freq": self.mel_min_max_freq
        }
        return parameters

    @classmethod
    def load_state_dict(cls, state_dict):
        """ load the dataset from previously saved parameters
        Args:
            state_dict: dict, parameter saved with state_dict function
        Returns:
            DESED class object with the right parameters
        """
        desed_obj = cls()
        desed_obj.feature_dir = state_dict["feature_dir"]
        desed_obj.meta_feat_dir = state_dict["meta_feat_dir"]
        desed_obj.compute_log = state_dict["compute_log"]
        desed_obj.sample_rate = state_dict["sample_rate"]
        desed_obj.n_window = state_dict["n_window"]
        desed_obj.hop_size = state_dict["hop_size"]
        desed_obj.n_mels = state_dict["n_mels"]
        desed_obj.mel_min_max_freq = state_dict["mel_min_max_freq"]
        return desed_obj

    def initialize_and_get_df(self, tsv_path, audio_dir=None, audio_dir_ss=None, pattern_ss=None,
                              ext_ss_feature_file="_ss", nb_files=None, download=True):
        """ Initialize the dataset, extract the features dataframes
        Args:
            tsv_path: str, tsv path in the initial dataset
            audio_dir: str, the path where to search the filename of the df
            audio_dir_ss: str, the path where to search the separated_sources
            pattern_ss: str, only when audio_dir_ss is not None, this should be defined
                the pattern that's added after normal filenames to get associated separated sources
            ext_ss_feature_file: str, only when audio_dir_ss is not None
            nb_files: int, optional, the number of file to take in the dataframe if taking a small part of the dataset.
            download: bool, optional, whether or not to download the data from the internet (youtube).

        Returns:
            pd.DataFrame
            The dataframe containing the right features and labels
        """
        # Parameters
        if audio_dir_ss is not None and pattern_ss is None:
            pattern_ss = "_events"
        if audio_dir is None:
            audio_dir = meta_path_to_audio_dir(tsv_path)
        # Path to save features, subdir, otherwise could have duplicate paths for synthetic data
        subdir = osp.sep.join(audio_dir.split(osp.sep)[-2:])
        meta_feat_dir = osp.join(self.meta_feat_dir, subdir)
        feature_dir = osp.join(self.feature_dir, subdir)
        os.makedirs(meta_feat_dir, exist_ok=True)
        os.makedirs(feature_dir, exist_ok=True)

        df_meta = self.get_df_from_meta(tsv_path, nb_files, pattern_ss=pattern_ss)
        logger.info("{} Total file number: {}".format(tsv_path, len(df_meta.filename.unique())))

        # Download real data
        if download:
            # Get only one filename once
            filenames = df_meta.filename.drop_duplicates()
            self.download(filenames, audio_dir)

        # Meta filename
        ext_tsv_feature = ""
        if audio_dir_ss is not None:
            ext_tsv_feature = ext_ss_feature_file
        fname, ext = osp.splitext(osp.basename(tsv_path))
        feat_fname = fname + ext_tsv_feature + ext
        if nb_files is not None:
            feat_fname = f"{nb_files}_{feat_fname}"
        features_tsv = osp.join(meta_feat_dir, feat_fname)

        # if not osp.exists(features_tsv):
        t = time.time()
        logger.info(f"Getting features ...")
        df_features = self.extract_features_from_df(df_meta, audio_dir, feature_dir,
                                                    audio_dir_ss, pattern_ss,
                                                    ext_ss_feature_file)
        if len(df_features) != 0:
            df_features.to_csv(features_tsv, sep="\t", index=False)
            logger.info(f"features created in {time.time() - t:.2f}s, metadata: {features_tsv}")
        else:
            raise IndexError(f"Empty features DataFrames {features_tsv}")
        # else:
        #     logger.info(f"loading features, \nmetadata: {features_tsv}")
        #     df_features = pd.read_csv(features_tsv, sep="\t")
        return df_features

    def calculate_mel_spec(self, audio, compute_log=False):
        """
        Calculate a mal spectrogram from raw audio waveform
        Note: The parameters of the spectrograms are in the config.py file.
        Args:
            audio : numpy.array, raw waveform to compute the spectrogram

        Returns:
            numpy.array
            containing the mel spectrogram
        """
        # Compute spectrogram
        ham_win = np.hamming(self.n_window)

        spec = librosa.stft(
            audio,
            n_fft=self.n_window,
            hop_length=self.hop_size,
            window=ham_win,
            center=True,
            pad_mode='reflect'
        )

        mel_spec = librosa.feature.melspectrogram(
            S=np.abs(spec),  # amplitude, for energy: spec**2 but don't forget to change amplitude_to_db.
            sr=self.sample_rate,
            n_mels=self.n_mels,
            fmin=self.mel_min_max_freq[0], fmax=self.mel_min_max_freq[1],
            htk=False, norm=None)

        if compute_log:
            mel_spec = librosa.amplitude_to_db(mel_spec)  # 10 * log10(S**2 / ref), ref default is 1
        mel_spec = mel_spec.T
        mel_spec = mel_spec.astype(np.float32)
        return mel_spec

    def load_and_compute_mel_spec(self, wav_path):
        (audio, _) = read_audio(wav_path, self.sample_rate)
        if audio.shape[0] == 0:
            raise IOError("File {wav_path} is corrupted!")
        else:
            t1 = time.time()
            mel_spec = self.calculate_mel_spec(audio, self.compute_log)
            logger.debug("compute features time: %s" % (time.time() - t1))
        return mel_spec

    def _extract_features(self, wav_path, out_path):
        if not osp.exists(out_path):
            try:
                mel_spec = self.load_and_compute_mel_spec(wav_path)
                os.makedirs(osp.dirname(out_path), exist_ok=True)
                np.save(out_path, mel_spec)
            except IOError as e:
                logger.error(e)

    def _extract_features_ss(self, wav_path, wav_paths_ss, out_path):
        if not osp.exists(out_path):
            try:
                features = np.expand_dims(self.load_and_compute_mel_spec(wav_path), axis=0)
                for wav_path_ss in wav_paths_ss:
                    sep_features = np.expand_dims(self.load_and_compute_mel_spec(wav_path_ss), axis=0)
                    features = np.concatenate((features, sep_features))
                os.makedirs(osp.dirname(out_path), exist_ok=True)
                np.save(out_path, features)
            except IOError as e:
                logger.error(e)

    def extract_features_from_df(self, df_meta, audio_dir, feature_dir, audio_dir_ss=None, pattern_ss=None,
                                 ext_ss_feature_file="_ss"):
        """Extract log mel spectrogram features.

        Args:
            df_meta : pd.DataFrame, containing at least column "filename" with name of the wav to compute features
            audio_dir: str, the path where to find the wav files specified by the dataframe
            audio_dir_ss: str, the path where to find the separated files (associated to the mixture)
            pattern_ss: str, the pattern following the normal filename to match the folder to find separated sources
            ext_ss_feature_file: str, only when audio_dir_ss is not None
        """
        if bool(audio_dir_ss) != bool(pattern_ss):
            raise NotImplementedError("if audio_dir_ss is not None, you must specify a pattern_ss")

        df_features = pd.DataFrame()
        fpaths = df_meta["filename"]
        uniq_fpaths = fpaths.drop_duplicates()

        for ind, filename in enumerate(uniq_fpaths):
            if ind % 500 == 0: logger.debug(ind)

            wav_path = osp.join(audio_dir, filename)
            if not osp.isfile(wav_path):
                logger.error("File %s is in the tsv file but the feature is not extracted because "
                             "file do not exist!" % wav_path)
                df_meta = df_meta.drop(df_meta[df_meta.filename == filename].index)
            else:
                if audio_dir_ss is None:
                    out_filename = osp.join(osp.splitext(filename)[0] + ".npy")
                    out_path = osp.join(feature_dir, out_filename)
                    self._extract_features(wav_path, out_path)
                else:
                    # To be changed if you have new separated sounds from the same mixture
                    out_filename = osp.join(osp.splitext(filename)[0] + ext_ss_feature_file + ".npy")
                    out_path = osp.join(feature_dir, out_filename)
                    bname, ext =  osp.splitext(filename)
                    wav_paths_ss = glob.glob(osp.join(audio_dir_ss, bname + pattern_ss, "*" + ext))
                    self._extract_features_ss(wav_path, wav_paths_ss, out_path)
                row_features = df_meta[df_meta.filename == filename]
                row_features.loc[:, "feature_filename"] = out_path
                df_features = df_features.append(row_features, ignore_index=True)

        return df_features.reset_index(drop=True)

    @staticmethod
    def get_classes(list_dfs):
        """ Get the different classes of the dataset
        Returns:
            A list containing the classes
        """
        classes = []
        for df in list_dfs:
            if "event_label" in df.columns:
                classes.extend(df["event_label"].dropna().unique())  # dropna avoid the issue between string and float
            elif "event_labels" in df.columns:
                classes.extend(df.event_labels.str.split(',', expand=True).unstack().dropna().unique())
        return list(set(classes))

    @staticmethod
    def get_subpart_data(df, nb_files, pattern_ss=None):
        """Get a subpart of a dataframe (only the number of files specified)
        Args:
            df : pd.DataFrame, the dataframe to extract a subpart of it (nb of filenames)
            nb_files: int, the number of file to take in the dataframe if taking a small part of the dataset.
            pattern_ss: str, if nb_files is not None, the pattern is needed to get same ss than soundscapes
        Returns:
            dataframe
        """
        column = "filename"
        if not nb_files > len(df[column].unique()):
            if pattern_ss is not None:
                filenames = df[column].apply(lambda x: x.split(pattern_ss)[0])
                filenames = filenames.drop_duplicates()
                # sort_values and random_state are used to have the same filenames each time (also for normal and ss)
                filenames_kept = filenames.sort_values().sample(nb_files, random_state=10)
                df_kept = df[df[column].apply(lambda x: x.split(pattern_ss)[0]).isin(filenames_kept)].reset_index(
                    drop=True)
            else:
                filenames = df[column].drop_duplicates()
                # sort_values and random_state are used to have the same filenames each time (also for normal and ss)
                filenames_kept = filenames.sort_values().sample(nb_files, random_state=10)
                df_kept = df[df[column].isin(filenames_kept)].reset_index(drop=True)

            logger.debug("Taking subpart of the data, len : {}, df_len: {}".format(nb_files, len(df)))
        else:
            df_kept = df
        return df_kept

    @staticmethod
    def get_df_from_meta(meta_name, nb_files=None, pattern_ss=None):
        """
        Extract a pandas dataframe from a tsv file

        Args:
            meta_name : str, path of the tsv file to extract the df
            nb_files: int, the number of file to take in the dataframe if taking a small part of the dataset.
            pattern_ss: str, if nb_files is not None, the pattern is needed to get same ss than soundscapes
        Returns:
            dataframe
        """
        df = pd.read_csv(meta_name, header=0, sep="\t")
        if nb_files is not None:
            df = DESED.get_subpart_data(df, nb_files, pattern_ss=pattern_ss)
        return df

    @staticmethod
    def download(filenames, audio_dir, n_jobs=3, chunk_size=10):
        """
        Download files contained in a list of filenames

        Args:
            filenames: list or pd.Series, filenames of files to be downloaded ()
            audio_dir: str, the directory where the wav file should be downloaded (if not exist)
            nb_files: int, the number of files to use, if a subpart of the dataframe wanted.
            chunk_size: int, (Default value = 10) number of files to download in a chunk
            n_jobs : int, (Default value = 3) number of parallel jobs
        """
        download(filenames, audio_dir, n_jobs=n_jobs, chunk_size=chunk_size)
