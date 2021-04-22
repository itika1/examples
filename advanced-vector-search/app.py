__copyright__ = "Copyright (c) 2021 Jina AI Limited. All rights reserved."
__license__ = "Apache-2.0"

from copy import copy
from itertools import tee

import click
import os

from collections import defaultdict
from functools import partial

from jina.flow import Flow
from jina import Document
from jina.logging.profile import TimeContext

from read_vectors_files import fvecs_read, ivecs_read


def general_config():
    os.environ['JINA_PARALLEL'] = os.environ.get('JINA_PARALLEL', '1')
    os.environ['JINA_SHARDS'] = os.environ.get('JINA_SHARDS', '2')
    os.environ['JINA_DATASET_NAME'] = os.environ.get('JINA_DATASET_NAME', 'siftsmall')
    os.environ['JINA_TMP_DATA_DIR'] = os.environ.get('JINA_TMP_DATA_DIR', './')
    os.environ['JINA_REQUEST_SIZE'] = os.environ.get('JINA_REQUEST_SIZE', '100')
    os.environ['OMP_NUM_THREADS'] = os.environ.get('OMP_NUM_THREADS', '1')


def query_config(indexer_query_type: str):
    if indexer_query_type == 'faiss':
        os.environ['JINA_USES'] = os.environ.get('JINA_USES_FAISS',
                                                 'docker://jinahub/pod.indexer.faissindexer:0.0.15-1.0.6')
        os.environ['JINA_USES_INTERNAL'] = 'yaml/faiss-indexer.yml'
        os.environ['JINA_FAISS_INDEX_KEY'] = os.environ.get('JINA_FAISS_INDEX_KEY',
                                                            'IVF10,PQ4')
        os.environ['JINA_FAISS_DISTANCE'] = os.environ.get('JINA_FAISS_DISTANCE',
                                                           'l2')
        os.environ['JINA_FAISS_NORMALIZE'] = os.environ.get('JINA_FAISS_NORMALIZE',
                                                            'False')
        os.environ['JINA_FAISS_NPROBE'] = os.environ.get('JINA_FAISS_NPROBE',
                                                         '1')
    elif indexer_query_type == 'annoy':
        os.environ['JINA_USES'] = os.environ.get('JINA_USES_ANNOY',
                                                 'docker://jinahub/pod.indexer.annoyindexer:0.0.16-1.0.6')
        os.environ['JINA_USES_INTERNAL'] = 'yaml/annoy-indexer.yml'
        os.environ['JINA_ANNOY_METRIC'] = os.environ.get('JINA_ANNOY_METRIC',
                                                         'euclidean')
        os.environ['JINA_ANNOY_NTREES'] = os.environ.get('JINA_ANNOY_NTREES',
                                                         '10')
        os.environ['JINA_ANNOY_SEARCH_K'] = os.environ.get('JINA_ANNOY_SEARCH_K',
                                                           '-1')
    elif indexer_query_type == 'numpy':
        os.environ['JINA_USES'] = 'yaml/indexer.yml'

    os.environ['JINA_DISTANCE_REVERSE'] = os.environ.get('JINA_DISTANCE_REVERSE',
                                                         'False')


def index_generator(db_file_path: str):
    documents = fvecs_read(db_file_path)
    for id, data in enumerate(documents):
        with Document() as doc:
            doc.content = data
            doc.tags['id'] = id
        yield doc


def index_restful(num_docs):
    f = Flow().load_config('flows/index.yml')

    with f:
        data_path = os.path.join(os.path.dirname(__file__), os.environ.get('JINA_DATA_FILE', None))
        print(f'Indexing {data_path}')
        url = f'http://0.0.0.0:{f.port_expose}/index'

        input_docs = _input_lines(
            filepath=data_path,
            size=num_docs,
            read_mode='r',
        )
        data_json = {'data': [Document(text=text).dict() for text in input_docs]}
        print(f'#### {len(data_json["data"])}')
        r = requests.post(url, json=data_json)
        if r.status_code != 200:
            raise Exception(f'api request failed, url: {url}, status: {r.status_code}, content: {r.content}')


def evaluate_generator(db_file_path: str, groundtruth_path: str):
    documents = fvecs_read(db_file_path)
    groundtruths = ivecs_read(groundtruth_path)

    for data_doc, gt_indexes in zip(documents, groundtruths):
        with Document() as doc:
            doc.content = data_doc
        with Document() as groundtruth:
            for index in gt_indexes:
                with Document() as match:
                    match.tags['id'] = int(index.item())
                groundtruth.matches.add(match)

        yield doc, groundtruth


def run(task, top_k, num_docs, indexer_query_type):
    general_config()
    query_config(indexer_query_type)

    request_size = int(os.environ['JINA_REQUEST_SIZE'])
    dataset_name = os.environ['JINA_DATASET_NAME']
    data_dir = os.path.join(dataset_name, os.environ['JINA_TMP_DATA_DIR'])

    if task == 'index':
        data_path = os.path.join(data_dir, f'{dataset_name}_base.fvecs')
        data_func = index_generator(data_path)
        data_func_list = list(data_func)

        with Flow.load_config('flow-index.yml') as flow:
            with TimeContext(f'QPS: indexing {len(list(data_func_list))}', logger=flow.logger):
                flow.index(input_fn=data_func_list, request_size=request_size)

    elif task == 'index_restful':
        index_restful(num_docs)

    elif task == 'query':
        evaluation_results = defaultdict(float)

        def _get_evaluation_results(evaluation_results: dict, resp):
            for d in resp.search.docs:
                for eval in d.evaluations:
                    evaluation_results[eval.op_name] = eval.value

        get_evaluation_results = partial(_get_evaluation_results, evaluation_results)

        data_path = os.path.join(data_dir, f'{dataset_name}_query.fvecs')
        groundtruth_path = os.path.join(data_dir, f'{dataset_name}_groundtruth.ivecs')
        query_input = list(evaluate_generator(data_path, groundtruth_path))

        with Flow.load_config('flow-query.yml') as flow:
            with TimeContext(f'QPS: query with {len(query_input)}', logger=flow.logger):
                flow.search(input_fn=query_input, request_size=request_size,
                            on_done=get_evaluation_results,
                            top_k=top_k)

        evaluation = evaluation_results[list(evaluation_results.keys())[0]]
        # return for test
        print(f'Recall@{top_k} ==> {100 * evaluation}')
        return 100 * evaluation
    else:
        raise NotImplementedError(
            f'unknown task: {task}. A valid task is either `index` or `query`.')


@click.command()
@click.option('--task', '-t')
@click.option('--top_k', '-k', default=100)
@click.option('--num_docs', '-n', default=500)
@click.option('--indexer-query-type', '-i', type=click.Choice(['faiss', 'annoy', 'numpy'], case_sensitive=False),
              default='faiss')
def main(task, top_k, num_docs, indexer_query_type):
    run(task, top_k, num_docs, indexer_query_type)


if __name__ == '__main__':
    main()
