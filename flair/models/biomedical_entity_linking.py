import os
import re
import pickle
import logging
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data.dataloader import DataLoader
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModel, default_data_collator
from huggingface_hub import hf_hub_url, cached_download
from string import punctuation
import flair
from flair.data import Sentence, EntityLinkingLabel
from typing import List, Union
from pathlib import Path

log = logging.getLogger("flair")


class SparseEncoder(object):
    def __init__(self, use_cuda=False):
        self.encoder = TfidfVectorizer(analyzer="char", ngram_range=(1, 2))
        self.use_cuda = use_cuda

    def fit(self, train_corpus):
        self.encoder.fit(train_corpus)
        return self

    def transform(self, mentions):
        vec = self.encoder.transform(mentions).toarray()
        vec = torch.FloatTensor(vec)  # return torch float tensor
        if self.use_cuda:
            vec = vec.cuda()
        return vec

    def cuda(self):
        self.use_cuda = True

        return self

    def cpu(self):
        self.use_cuda = False
        return self

    def __call__(self, mentions):
        return self.transform(mentions)

    def vocab(self):
        return self.encoder.vocabulary_

    def save_encoder(self, path):
        with open(path, "wb") as fout:
            pickle.dump(self.encoder, fout)
            log.info("Sparse encoder saved in {}".format(path))

    def load_encoder(self, path):
        with open(path, "rb") as fin:
            self.encoder = pickle.load(fin)
            log.info("Sparse encoder loaded from {}".format(path))

        return self


class TextPreprocess:
    """
    Text Preprocess module
    Support lowercase, removing punctuation, typo correction
    """

    def __init__(
        self,
        lowercase=True,
        remove_punctuation=True,
        ignore_punctuations="",
    ):
        """
        :param typo_path str: path of known typo dictionary
        """
        self.lowercase = lowercase
        self.rmv_puncts = remove_punctuation
        self.punctuation = punctuation
        for ig_punc in ignore_punctuations:
            self.punctuation = self.punctuation.replace(ig_punc, "")
        self.rmv_puncts_regex = re.compile(
            r"[\s{}]+".format(re.escape(self.punctuation))
        )


    def remove_punctuation(self, phrase):
        phrase = self.rmv_puncts_regex.split(phrase)
        phrase = " ".join(phrase).strip()

        return phrase


    def run(self, text):
        if self.lowercase:
            text = text.lower()

        if self.rmv_puncts:
            text = self.remove_punctuation(text)

        text = text.strip()

        return text


class NamesDataset(torch.utils.data.Dataset):
    def __init__(self, encodings):
        self.encodings = encodings

    def __getitem__(self, idx):
        return {key: val[idx].clone().detach() for key, val in self.encodings.items()}
        # This gave a warning
        # return {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}

    def __len__(self):
        return len(self.encodings.input_ids)


class DictionaryDataset:
    """
    A class used to load dictionary data
    """

    def __init__(self, dictionary_path):
        """
        :param dictionary_path str: The path of the dictionary
        """
        log.info("Loading Dictionary from {}".format(dictionary_path))
        self.data = self.load_data(dictionary_path)

    def load_data(self, dictionary_path):
        data = []
        with open(dictionary_path, mode="r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in tqdm(lines):
                line = line.strip()
                if line == "":
                    continue
                cui, name = line.split("||")
                name = name.lower()
                data.append((name, cui))

        data = np.array(data)
        return data


class BioSyn(object):
    """
    Wrapper class for dense encoder and sparse encoder
    """

    def __init__(self, max_length, use_cuda):
        self.max_length = max_length
        self.use_cuda = use_cuda

        self.tokenizer = None
        self.encoder = None
        self.sparse_encoder = None
        self.sparse_weight = None

    def get_sparse_weight(self):
        assert self.sparse_weight is not None

        return self.sparse_weight

    def load_model(self, model_name_or_path):
        self.load_dense_encoder(model_name_or_path)
        self.load_sparse_encoder(model_name_or_path)
        self.load_sparse_weight(model_name_or_path)

        return self

    def load_dense_encoder(self, model_name_or_path):
        self.encoder = AutoModel.from_pretrained(model_name_or_path)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
        if self.use_cuda:
            self.encoder = self.encoder.to("cuda")

        return self.encoder, self.tokenizer

    def load_sparse_encoder(self, model_name_or_path):
        sparse_encoder_path = os.path.join(model_name_or_path, "sparse_encoder.pk")
        # check file exists
        if not os.path.isfile(sparse_encoder_path):
            # download from huggingface hub and cache it
            sparse_encoder_url = hf_hub_url(
                model_name_or_path, filename="sparse_encoder.pk"
            )
            sparse_encoder_path = cached_download(
                url=sparse_encoder_url,
                cache_dir=flair.cache_root / "models" / model_name_or_path)

        self.sparse_encoder = SparseEncoder().load_encoder(path=sparse_encoder_path)

        return self.sparse_encoder

    def load_sparse_weight(self, model_name_or_path):
        sparse_weight_path = os.path.join(model_name_or_path, "sparse_weight.pt")
        # check file exists
        if not os.path.isfile(sparse_weight_path):
            # download from huggingface hub and cache it
            sparse_weight_url = hf_hub_url(
                model_name_or_path, filename="sparse_weight.pt"
            )
            sparse_weight_path = cached_download(
                url=sparse_weight_url,
                cache_dir=flair.cache_root / "models" / model_name_or_path
            )

        self.sparse_weight = torch.load(sparse_weight_path)

        return self.sparse_weight

    def get_score_matrix(self, query_embeds, dict_embeds):
        """
        Return score matrix
        :param query_embeds np.array: 2d numpy array of query embeddings
        :param dict_embeds np.array: 2d numpy array of query embeddings
        :returns score_matrix np.array: 2d numpy array of scores
        """
        score_matrix = np.matmul(query_embeds, dict_embeds.T)

        return score_matrix

    def retrieve_candidate(self, score_matrix, topk):
        """
        Return sorted topk indxes (descending order)
        :param score_matrix np.array: 2d numpy array of scores
        :param topk int: The number of candidates
        :returns topk_idxs np.array: 2d numpy array of scores [# of query , # of dict]
        """

        def indexing_2d(arr, cols):
            rows = np.repeat(
                np.arange(0, cols.shape[0])[:, np.newaxis], cols.shape[1], axis=1
            )
            return arr[rows, cols]

        # get topk indexes without sorting
        topk_idxs = np.argpartition(score_matrix, -topk)[:, -topk:]

        # get topk indexes with sorting
        topk_score_matrix = indexing_2d(score_matrix, topk_idxs)
        topk_argidxs = np.argsort(-topk_score_matrix)
        topk_scores = np.sort(topk_score_matrix)
        topk_idxs = indexing_2d(topk_idxs, topk_argidxs)

        return topk_idxs, topk_scores

    def embed_sparse(self, names, show_progress=False):
        """
        Embedding data into sparse representations
        :param names np.array: An array of names
        :returns sparse_embeds np.array: A list of sparse embeddings
        """
        batch_size = 1024
        sparse_embeds = []

        if show_progress:
            iterations = tqdm(range(0, len(names), batch_size))
        else:
            iterations = range(0, len(names), batch_size)

        for start in iterations:
            end = min(start + batch_size, len(names))
            batch = names[start:end]
            batch_sparse_embeds = self.sparse_encoder(batch)
            batch_sparse_embeds = batch_sparse_embeds.numpy()
            sparse_embeds.append(batch_sparse_embeds)
        sparse_embeds = np.concatenate(sparse_embeds, axis=0)

        return sparse_embeds

    def embed_dense(self, names, show_progress=False):
        """
        Embedding data into dense representations
        :param names np.array or list: An array of names
        :returns dense_embeds list: A list of dense embeddings
        """
        self.encoder.eval()  # prevent dropout

        batch_size = 1024
        dense_embeds = []

        if isinstance(names, np.ndarray):
            names = names.tolist()
        name_encodings = self.tokenizer(
            names,
            padding="max_length",
            max_length=self.max_length,
            truncation=True,
            return_tensors="pt",
        )
        if self.use_cuda:
            name_encodings = name_encodings.to("cuda")
        name_dataset = NamesDataset(name_encodings)
        name_dataloader = DataLoader(
            name_dataset,
            shuffle=False,
            collate_fn=default_data_collator,
            batch_size=batch_size,
        )

        with torch.no_grad():
            for batch in tqdm(
                name_dataloader, disable=not show_progress, desc="embedding dictionary"
            ):
                outputs = self.encoder(**batch)
                batch_dense_embeds = (
                    outputs[0][:, 0].cpu().detach().numpy()
                )  # [CLS] representations
                dense_embeds.append(batch_dense_embeds)
        dense_embeds = np.concatenate(dense_embeds, axis=0)

        return dense_embeds

    def get_predictions(self, mention, dictionary, dict_sparse_embeds, dict_dense_embeds, topk, tgt_space_mean_vec=None):
        # embed mention
        mention_sparse_embeds = self.embed_sparse(names=[mention])
        mention_dense_embeds = self.embed_dense(names=[mention])

        # calcuate score matrix and get top 5
        sparse_score_matrix = self.get_score_matrix(
            query_embeds=mention_sparse_embeds, dict_embeds=dict_sparse_embeds
        )
        dense_score_matrix = self.get_score_matrix(
            query_embeds=mention_dense_embeds, dict_embeds=dict_dense_embeds
        )
        sparse_weight = self.get_sparse_weight().item()
        hybrid_score_matrix = sparse_weight * sparse_score_matrix + dense_score_matrix
        hybrid_candidate_idxs, hybrid_candidate_scores = self.retrieve_candidate(
            score_matrix=hybrid_score_matrix, topk=topk
        )
        # return dictionary[hybrid_candidate_idxs].squeeze(0)
        return [np.append(dictionary[ind], score) for ind, score in zip(hybrid_candidate_idxs[0].tolist(), hybrid_candidate_scores[0].tolist())]


class SapBert(object):
    """
    Wrapper class for BERT encoder
    """

    # Same as BioSyn
    def __init__(self, max_length, use_cuda):
        self.max_length = max_length
        self.use_cuda = use_cuda

        self.tokenizer = None
        self.encoder = None

    # Differenece to BioSyn: not loading sparse encoder and weights
    def load_model(self, model_name_or_path):
        self.load_bert(model_name_or_path)
        
        return self

    # Difference to load_dense_encoder in BioSyn: use_fast and do_lower_case
    def load_bert(self, path, lowercase=True):
        self.tokenizer = AutoTokenizer.from_pretrained(path, 
                use_fast=True, do_lower_case=lowercase)
        self.encoder = AutoModel.from_pretrained(path)
        if self.use_cuda:
            self.encoder = self.encoder.cuda()

        return self.encoder, self.tokenizer
    
    # Difference to BioSyn: has parameters cosine and normalize and uses them, same when not cosine and not normalize
    def get_score_matrix(self, query_embeds, dict_embeds, cosine=False, normalise=False):
        """
        Return score matrix

        Parameters
        ----------
        query_embeds : np.array
            2d numpy array of query embeddings
        dict_embeds : np.array
            2d numpy array of query embeddings

        Returns
        -------
        score_matrix : np.array
            2d numpy array of scores
        """
        if cosine:
            score_matrix = cosine_similarity(query_embeds, dict_embeds)
        else:
            score_matrix = np.matmul(query_embeds, dict_embeds.T)

        if normalise:
            score_matrix = (score_matrix - score_matrix.min() ) / (score_matrix.max() - score_matrix.min())
        
        return score_matrix

    # same as in BioSyn
    def retrieve_candidate(self, score_matrix, topk):
        """
        Return sorted topk idxes (descending order)

        Parameters
        ----------
        score_matrix : np.array
            2d numpy array of scores
        topk : int
            The number of candidates

        Returns
        -------
        topk_idxs : np.array
            2d numpy array of scores [# of query , # of dict]
        """
        
        def indexing_2d(arr, cols):
            rows = np.repeat(np.arange(0,cols.shape[0])[:, np.newaxis],cols.shape[1],axis=1)
            return arr[rows, cols]

        # get topk indexes without sorting
        topk_idxs = np.argpartition(score_matrix,-topk)[:, -topk:]

        # get topk indexes with sorting
        topk_score_matrix = indexing_2d(score_matrix, topk_idxs)
        topk_argidxs = np.argsort(-topk_score_matrix)
        topk_scores = np.sort(topk_score_matrix)
        topk_idxs = indexing_2d(topk_idxs, topk_argidxs)

        return topk_idxs, topk_scores

    # Not in BioSyn
    def retrieve_candidate_cuda(self, score_matrix, topk, batch_size=128, show_progress=False):
        """
        Return sorted topk idxes (descending order)

        Parameters
        ----------
        score_matrix : np.array
            2d numpy array of scores
        topk : int
            The number of candidates

        Returns
        -------
        topk_idxs : np.array
            2d numpy array of scores [# of query , # of dict]
        """

        res = None
        scores = None
        for i in tqdm(np.arange(0, score_matrix.shape[0], batch_size), disable=not show_progress):
            score_matrix_tmp = torch.tensor(score_matrix[i:i+batch_size]).cuda()
            sorted_values, matrix_sorted = torch.sort(score_matrix_tmp, dim=1, descending=True)
            matrix_sorted = matrix_sorted[:, :topk].cpu()
            sorted_values = sorted_values[:, :topk].cpu()
            if res is None: 
                res = matrix_sorted
                scores = sorted_values
            else:
                res = torch.cat([res, matrix_sorted], axis=0)
                scores = torch.cat([scores, sorted_values], axis=0)

        return res.numpy(), scores.numpy()

    def embed_sparse(self, names, show_progress):
        return []

    # Attention: uses cuda
    # Differnece: parameters batch_size (in BioSyn as constant) and agg_mode
    # different code
    def embed_dense(self, names, show_progress=False, batch_size=2048, agg_mode="cls"):
        """
        Embedding data into dense representations

        Parameters
        ----------
        names : np.array
            An array of names

        Returns
        -------
        dense_embeds : list
            A list of dense embeddings
        """
        self.encoder.eval() # prevent dropout
        
        # Difference: batch size given as parameter
        batch_size=batch_size
        dense_embeds = []


        with torch.no_grad():
            if show_progress:
                iterations = tqdm(range(0, len(names), batch_size))
            else:
                iterations = range(0, len(names), batch_size)
                
            for start in iterations:
                end = min(start + batch_size, len(names))
                batch = names[start:end]
                batch_tokenized_names = self.tokenizer.batch_encode_plus(
                        batch, add_special_tokens=True, 
                        truncation=True, max_length=self.max_length, 
                        padding="max_length", return_tensors='pt')
                batch_tokenized_names_cuda = {}
                for k,v in batch_tokenized_names.items(): 
                    batch_tokenized_names_cuda[k] = v.cuda()
                
                last_hidden_state = self.encoder(**batch_tokenized_names_cuda)[0]
                if agg_mode == "cls":
                    batch_dense_embeds = last_hidden_state[:,0,:] # [CLS]
                elif agg_mode == "mean_all_tok":
                    batch_dense_embeds = last_hidden_state.mean(1) # pooling
                elif agg_mode == "mean":
                    batch_dense_embeds = (last_hidden_state * batch_tokenized_names_cuda['attention_mask'].unsqueeze(-1)).sum(1) / batch_tokenized_names_cuda['attention_mask'].sum(-1).unsqueeze(-1)
                else:
                    print ("no such agg_mode:", agg_mode)
                    batch_dense_embeds = []

                batch_dense_embeds = batch_dense_embeds.cpu().detach().numpy()
                dense_embeds.append(batch_dense_embeds)
        dense_embeds = np.concatenate(dense_embeds, axis=0)
        
        return dense_embeds

    # possible values for agg_mode: cls|mean_pool|nospec
    def get_predictions(self, mention, dictionary, dict_sparse_embeds, dict_dense_embeds, topk, tgt_space_mean_vec=None, agg_mode="cls"):
        mention_dense_embeds = self.embed_dense(names=[mention], agg_mode=agg_mode)
        # should I leave this in?
        if tgt_space_mean_vec is not None:
            mention_dense_embeds -= tgt_space_mean_vec

        # get score matrix
        # same as in BioSyn, but wihtout the sparse encoder
        dense_score_matrix = self.get_score_matrix(
                query_embeds=mention_dense_embeds, 
                dict_embeds=dict_dense_embeds,
        )
        score_matrix = dense_score_matrix
        # same as in BioSyn, but with options for batchsize and show_progress
        # TODO: Should differentiate based on cuda availability!
        candidate_idxs, candidate_scores = self.retrieve_candidate_cuda(
                score_matrix = score_matrix, 
                topk = topk,
                batch_size=16,
                show_progress=False
        )
        # build return value with array of [concept_name, cui, score]
        return [np.append(dictionary[ind], score) for ind, score in zip(candidate_idxs[0].tolist(), candidate_scores[0].tolist())]#.squeeze()


class HunNen(object):
    """
    Biomedical Entity Linker for HunFlair
    Can predict top k entities on sentences annotated with biomedical entity mentions using BioSyn.
    """

    def __init__(self, model, dictionary: DictionaryDataset, dict_sparse_embeds, dict_dense_embeds, tgt_space_mean_vec):
        """
        Initalize HunNen class, called by classmethod load
        :param model: BioSyn object containing the dense and sparse encoders
        :param dictionary: numpy array containing all concept names and their cui
        :param dict_sparse_embeds: sparse embeddings of dictionary
        :param dict_dense_embeds: dense embeddings of dictionary
        """
        super().__init__()
        self.model = model
        self.dictionary = dictionary
        self.dict_sparse_embeds = dict_sparse_embeds
        self.dict_dense_embeds = dict_dense_embeds
        self.tgt_space_mean_vec = tgt_space_mean_vec
        
    @classmethod
    def load(cls, model_name, dictionary_path:  Union[str, Path], model_type, max_length=25):
        """
        Load a model for biomedical named entity normalization using BioSyn on sentences annotated with 
        biomedical entity mentions
        :param model_name: Name of pretrained model to use. Currently possible values for pretrained models are: 
        sapbert-bc5cdr-disease, sapbert-ncbi-disease, sapbert-bc5cdr-chemical, biobert-bc5cdr-disease, 
        biobert-ncbi-disease, biobert-bc5cdr-chemical, sapbert
        :param dictionary_path: Path to a file with each line in the format: cui||name, with one line for each
        name of a concept
        """
        # Use BioSyn
        if model_type.lower() == "biosyn":
            # modify name if it's one of the BioSyn huggingface models
            if model_name in ["sapbert-bc5cdr-disease", "sapbert-ncbi-disease", "sapbert-bc5cdr-chemical", 
            "biobert-bc5cdr-disease", "biobert-ncbi-disease", "biobert-bc5cdr-chemical"]:
                model_name = "dmis-lab/biosyn-" + model_name
            model = BioSyn(max_length=max_length, use_cuda=torch.cuda.is_available())



        # Use SapBert
        elif model_type.lower() == "sapbert":
            if model_name == "sapbert":
                model_name = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
            model = SapBert(max_length=max_length, use_cuda=torch.cuda.is_available())

        else:
            print("Invalid value for model_type. The only possible values are 'BioSyn' and 'SapBert'")
            return        

        model.load_model(model_name_or_path=model_name)
        # cache or load dictionary
        dictionary, dict_sparse_embeds, dict_dense_embeds, tgt_space_mean_vec = cls._cache_or_load_dictionary(
            model, model_name, str(dictionary_path)
        )

        return cls(model, dictionary, dict_sparse_embeds, dict_dense_embeds, tgt_space_mean_vec);

    def predict(self, sentences: Union[List[Sentence], Sentence], entity_type, topk = 10):
        """
        On one or more sentences, predict the cui on all named entites annotated with a tag of type entity_type. 
        Annotates the top k predictions.
        :param sentences: one or more sentences to run the predictions on
        :param entity_type: only entities with this tag will be annotated
        :param topk: number of predicted cui candidates to add to annotation
        """
        # make sure its a list of sentences
        if not isinstance(sentences, list):
            sentences = [sentences]
        
        for sentence in sentences:
            for entity in sentence.get_labels(entity_type):
                # preprocess mention
                mention = TextPreprocess().run(entity.span.text)

                # get predictions from dictionary
                predictions = self.model.get_predictions(
                    mention, 
                    self.dictionary, 
                    self.dict_sparse_embeds, 
                    self.dict_dense_embeds, 
                    topk,
                    self.tgt_space_mean_vec
                )

                for prediction in predictions:
                    sentence.add_complex_label(typename=entity_type + "_nen", 
                        label=EntityLinkingLabel(span=entity.span, cui = prediction[1], concept_name = prediction[0], score = prediction[2].astype(float)))


    @staticmethod
    def _cache_or_load_dictionary(entity_linker, model_name_or_path, dictionary_path, mean_centering=False):
        tgt_space_mean_vec = None
        dictionary_name = os.path.splitext(os.path.basename(dictionary_path))[0]

        cache_folder = os.path.join(flair.cache_root, "datasets")
        cached_dictionary_path = os.path.join(
            cache_folder, f"cached_{model_name_or_path.split('/')[-1]}_{dictionary_name}.pk"
        )

        # If exist, load the cached dictionary
        if os.path.exists(cached_dictionary_path):
            with open(cached_dictionary_path, "rb") as fin:
                cached_dictionary = pickle.load(fin)
            log.info("Loaded dictionary from cached file {}".format(cached_dictionary_path))

            dictionary, dict_sparse_embeds, dict_dense_embeds = (
                cached_dictionary["dictionary"],
                cached_dictionary["dict_sparse_embeds"],
                cached_dictionary["dict_dense_embeds"],
            )

        else:
            dictionary = DictionaryDataset(dictionary_path=dictionary_path).data
            # dictionary_names = dictionary[:, 0]
            dictionary_names = [row[0] for row in dictionary]
            dict_sparse_embeds = entity_linker.embed_sparse(
                names=dictionary_names, show_progress=True
            )
            dict_dense_embeds = entity_linker.embed_dense(
                names=dictionary_names, show_progress=True
            )

            if mean_centering:
                tgt_space_mean_vec = dict_dense_embeds.mean(0)
                dict_dense_embeds -= tgt_space_mean_vec
            cached_dictionary = {
                "dictionary": dictionary,
                "dict_sparse_embeds": dict_sparse_embeds,
                "dict_dense_embeds": dict_dense_embeds,
            }

            if not os.path.exists(cache_folder):
                os.mkdir(cache_folder)
            with open(cached_dictionary_path, "wb") as fin:
                pickle.dump(cached_dictionary, fin)
            print("Saving dictionary into cached file {}".format(cached_dictionary_path))

        return dictionary, dict_sparse_embeds, dict_dense_embeds, tgt_space_mean_vec