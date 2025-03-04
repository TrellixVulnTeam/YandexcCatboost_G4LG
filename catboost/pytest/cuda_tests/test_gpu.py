import catboost
import csv
import filecmp
import json
import numpy as np
import os
import pytest
import re
import yatest.common

from copy import deepcopy
from catboost_pytest_lib import (
    append_params_to_cmdline,
    apply_catboost,
    data_file,
    execute,
    execute_catboost_fit,
    get_limited_precision_dsv_diff_tool,
    local_canonical_file,
)

CATBOOST_PATH = yatest.common.binary_path("catboost/app/catboost")
BOOSTING_TYPE = ['Ordered', 'Plain']
MULTICLASS_LOSSES = ['MultiClass', 'MultiClassOneVsAll']


def generate_random_labeled_set(nrows, nvals, labels, seed=20181219, prng=None):
    if prng is None:
        prng = np.random.RandomState(seed=seed)
    label = prng.choice(labels, [nrows, 1])
    feature = prng.random_sample([nrows, nvals])
    return np.concatenate([label, feature], axis=1)


BY_CLASS_METRICS = ['AUC', 'Precision', 'Recall', 'F1']


def compare_evals(custom_metric, fit_eval, calc_eval, eps=1e-7):
    csv_fit = csv.reader(open(fit_eval, "r"), dialect='excel-tab')
    csv_calc = csv.reader(open(calc_eval, "r"), dialect='excel-tab')

    head_fit = next(csv_fit)
    head_calc = next(csv_calc)

    if isinstance(custom_metric, basestring):
        custom_metric = [custom_metric]

    for metric_name in deepcopy(custom_metric):
        if metric_name in BY_CLASS_METRICS:
            custom_metric.remove(metric_name)

            for fit_metric_name in head_fit:
                if fit_metric_name[:len(metric_name)] == metric_name:
                    custom_metric.append(fit_metric_name)

    col_idx_fit = {}
    col_idx_calc = {}

    for metric_name in custom_metric:
        col_idx_fit[metric_name] = head_fit.index(metric_name)
        col_idx_calc[metric_name] = head_calc.index(metric_name)

    while True:
        try:
            line_fit = next(csv_fit)
            line_calc = next(csv_calc)
            for metric_name in custom_metric:
                fit_value = float(line_fit[col_idx_fit[metric_name]])
                calc_value = float(line_calc[col_idx_calc[metric_name]])
                max_abs = max(abs(fit_value), abs(calc_value))
                err = abs(fit_value - calc_value) / max_abs if max_abs > 0 else 0
                if err > eps:
                    raise Exception('{}, iter {}: fit vs calc = {} vs {}, err = {} > eps = {}'.format(
                        metric_name, line_fit[0], fit_value, calc_value, err, eps))
        except StopIteration:
            break


def diff_tool(threshold=2e-7):
    return get_limited_precision_dsv_diff_tool(threshold, True)


@pytest.fixture(scope='module', autouse=True)
def skipif_no_cuda():
    for flag in pytest.config.option.flags:
        if re.match('HAVE_CUDA=(0|no|false)', flag, flags=re.IGNORECASE):
            return pytest.mark.skipif(True, reason=flag)

    return pytest.mark.skipif(False, reason='None')


pytestmark = skipif_no_cuda()


def fit_catboost_gpu(params, devices='0', input_data=None, output_data=None):
    execute_catboost_fit(
        task_type='GPU',
        params=params,
        devices=devices,
        input_data=input_data,
        output_data=output_data
    )


# currently only works on CPU
def fstr_catboost_cpu(params):
    cmd = list()
    cmd.append(CATBOOST_PATH)
    cmd.append('fstr')
    append_params_to_cmdline(cmd, params)
    execute(cmd)


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
@pytest.mark.parametrize('qwise_loss', ['QueryRMSE', 'RMSE'])
def test_queryrmse(boosting_type, qwise_loss):
    output_model_path = yatest.common.test_output_path('model.bin')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    predictions_path_learn = yatest.common.test_output_path('predictions_learn.tsv')
    predictions_path_test = yatest.common.test_output_path('predictions_test.tsv')

    learn_file = data_file('querywise', 'train')
    cd_file = data_file('querywise', 'train.cd')
    test_file = data_file('querywise', 'test')
    params = {"--loss-function": qwise_loss,
              "-f": learn_file,
              "-t": test_file,
              '--column-description': cd_file,
              '--boosting-type': boosting_type,
              '-i': '100',
              '-T': '4',
              '-m': output_model_path,
              '--learn-err-log': learn_error_path,
              '--test-err-log': test_error_path,
              '--use-best-model': 'false'
              }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, learn_file, cd_file, predictions_path_learn)
    apply_catboost(output_model_path, test_file, cd_file, predictions_path_test)

    return [local_canonical_file(learn_error_path, diff_tool=diff_tool()),
            local_canonical_file(test_error_path, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_learn, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_test, diff_tool=diff_tool()),
            ]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_boosting_type(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    train_file = data_file('adult', 'train_small')
    test_file = data_file('adult', 'test_small')
    cd_file = data_file('adult', 'train.cd')

    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': train_file,
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '-m': output_model_path,
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


def combine_dicts(first, *vargs):
    combined = first.copy()
    for rest in vargs:
        combined.update(rest)
    return combined


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_bootstrap(boosting_type):
    bootstrap_option = {
        'no': {'--bootstrap-type': 'No'},
        'bayes': {'--bootstrap-type': 'Bayesian', '--bagging-temperature': '0.0'},
        'bernoulli': {'--bootstrap-type': 'Bernoulli', '--subsample': '1.0'}
    }

    test_file = data_file('adult', 'test_small')
    cd_file = data_file('adult', 'train.cd')

    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
    }

    for bootstrap in bootstrap_option:
        model_path = yatest.common.test_output_path('model_' + bootstrap + '.bin')
        eval_path = yatest.common.test_output_path('test_' + bootstrap + '.eval')
        model_option = {'-m': model_path}

        run_params = combine_dicts(params,
                                   bootstrap_option[bootstrap],
                                   model_option)

        fit_catboost_gpu(run_params)
        apply_catboost(model_path, test_file, cd_file, eval_path)

    ref_eval_path = yatest.common.test_output_path('test_no.eval')
    assert (filecmp.cmp(ref_eval_path, yatest.common.test_output_path('test_bayes.eval')))
    assert (filecmp.cmp(ref_eval_path, yatest.common.test_output_path('test_bernoulli.eval')))

    return [local_canonical_file(ref_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_nan_mode_forbidden(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    test_file = data_file('adult', 'test_small')
    learn_file = data_file('adult', 'train_small')
    cd_file = data_file('adult', 'train.cd')
    params = {
        '-f': learn_file,
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '20',
        '-T': '4',
        '-m': output_model_path,
        '--nan-mode': 'Forbidden',
        '--use-best-model': 'false',
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_overfit_detector_iter(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '2000',
        '-T': '4',
        '-m': output_model_path,
        '-x': '1',
        '-n': '8',
        '-w': '0.5',
        '--od-type': 'Iter',
        '--od-wait': '2',
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_overfit_detector_inc_to_dec(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '2000',
        '-T': '4',
        '-m': output_model_path,
        '-x': '1',
        '-n': '8',
        '-w': '0.5',
        '--od-pval': '0.5',
        '--od-type': 'IncToDec',
        '--od-wait': '2',
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


NAN_MODE = ['Min', 'Max']


@pytest.mark.parametrize('nan_mode', NAN_MODE)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_nan_mode(nan_mode, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    test_file = data_file('adult_nan', 'test_small')
    cd_file = data_file('adult_nan', 'train.cd')

    params = {
        '--use-best-model': 'false',
        '-f': data_file('adult_nan', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '20',
        '-T': '4',
        '-m': output_model_path,
        '--nan-mode': nan_mode
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_use_best_model(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = {
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '100',
        '-T': '4',
        '-m': output_model_path,
        '-x': '1',
        '-n': '8',
        '-w': '1',
        '--od-pval': '0.99',
        '--use-best-model': 'true'
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


LOSS_FUNCTIONS = ['RMSE', 'Logloss', 'MAE', 'CrossEntropy', 'Quantile', 'LogLinQuantile', 'Poisson', 'MAPE']
LEAF_ESTIMATION_METHOD = ['Gradient', 'Newton']


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_crossentropy(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    cd_file = data_file('adult_crossentropy', 'train.cd')
    test_file = data_file('adult_crossentropy', 'test_proba')
    params = {
        '--loss-function': 'CrossEntropy',
        '-f': data_file('adult_crossentropy', 'train_proba'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '-m': output_model_path,
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_permutation_block(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    cd_file = data_file('adult_crossentropy', 'train.cd')
    test_file = data_file('adult_crossentropy', 'test_proba')
    params = {
        '--loss-function': 'CrossEntropy',
        '-f': data_file('adult_crossentropy', 'train_proba'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '--fold-permutation-block': '8',
        '-m': output_model_path,
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_ignored_features(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    test_file = data_file('adult', 'test_small')
    cd_file = data_file('adult', 'train.cd')
    params = {
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '-m': output_model_path,
        '-I': '0:1:3:5-7:10000',
        '--use-best-model': 'false',
    }

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


def test_ignored_features_not_read():
    output_model_path = yatest.common.test_output_path('model.bin')
    input_cd_path = data_file('adult', 'train.cd')
    cd_file = yatest.common.test_output_path('train.cd')

    with open(input_cd_path, "rt") as f:
        cd_lines = f.readlines()
    with open(cd_file, "wt") as f:
        for cd_line in cd_lines:
            # Corrupt some features by making them 'Num'
            if cd_line.split() == ('5', 'Categ'):  # column 5 --> feature 4
                cd_line = cd_line.replace('Categ', 'Num')
            if cd_line.split() == ('7', 'Categ'):  # column 7 --> feature 6
                cd_line = cd_line.replace('Categ', 'Num')
            f.write(cd_line)

    test_file = data_file('adult', 'test_small')
    params = {
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': output_model_path,
        '-I': '4:6',
        '--use-best-model': 'false',
    }

    fit_catboost_gpu(params)


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_baseline(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    cd_file = data_file('train_adult_baseline.cd')
    test_file = data_file('adult_weight', 'test_weight')
    params = {
        '--loss-function': 'Logloss',
        '-f': data_file('adult_weight', 'train_weight'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '-m': output_model_path,
        '--use-best-model': 'false',
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_weights(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult_weight', 'train.cd')
    test_file = data_file('adult_weight', 'test_weight')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult_weight', 'train_weight'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_weights_without_bootstrap(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult_weight', 'train.cd')
    test_file = data_file('adult_weight', 'test_weight')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult_weight', 'train_weight'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '--bootstrap-type': 'No',
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
@pytest.mark.parametrize('leaf_estimation', ["Newton", "Gradient"])
def test_weighted_pool_leaf_estimation_method(boosting_type, leaf_estimation):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult_weight', 'train.cd')
    test_file = data_file('adult_weight', 'test_weight')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult_weight', 'train_weight'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-T': '4',
        '--leaf-estimation-method': leaf_estimation,
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
@pytest.mark.parametrize('leaf_estimation', ["Newton", "Gradient"])
def test_leaf_estimation_method(boosting_type, leaf_estimation):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-T': '4',
        '--leaf-estimation-method': leaf_estimation,
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_one_hot_max_size(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '--one-hot-max-size': 64,
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_l2_reg_size(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-T': '4',
        '--l2-leaf-reg': 10,
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_has_time(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult', 'train.cd')
    test_file = data_file('adult', 'test_small')
    params = (
        '--use-best-model', 'false',
        '--loss-function', 'Logloss',
        '-f', data_file('adult', 'train_small'),
        '-t', test_file,
        '--column-description', cd_file,
        '--boosting-type', boosting_type,
        '-i', '10',
        '-w', '0.03',
        '-T', '4',
        '--has-time',
        '-m', output_model_path,
    )
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_logloss_with_not_binarized_target(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult_not_binarized', 'train.cd')
    test_file = data_file('adult_not_binarized', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult_not_binarized', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': boosting_type,
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


def test_fold_len_mult():
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult_not_binarized', 'train.cd')
    test_file = data_file('adult_not_binarized', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult_not_binarized', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': 'Ordered',
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '--fold-len-multiplier': 1.2,
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


def test_random_strength():
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')
    cd_file = data_file('adult_not_binarized', 'train.cd')
    test_file = data_file('adult_not_binarized', 'test_small')
    params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': data_file('adult_not_binarized', 'train_small'),
        '-t': test_file,
        '--column-description': cd_file,
        '--boosting-type': 'Ordered',
        '-i': '10',
        '-w': '0.03',
        '-T': '4',
        '--random-strength': 122,
        '-m': output_model_path,
    }
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('loss_function', LOSS_FUNCTIONS)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_all_targets(loss_function, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    test_file = data_file('adult', 'test_small')
    cd_file = data_file('adult', 'train.cd')
    params = (
        '--use-best-model', 'false',
        '--loss-function', loss_function,
        '-f', data_file('adult', 'train_small'),
        '-t', test_file,
        '--column-description', cd_file,
        '--boosting-type', boosting_type,
        '-i', '10',
        '-w', '0.03',
        '-T', '4',
        '-m', output_model_path,
    )

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('is_inverted', [False, True], ids=['', 'inverted'])
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_cv(is_inverted, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    params = (
        '--use-best-model', 'false',
        '--loss-function', 'Logloss',
        '-f', data_file('adult', 'train_small'),
        '--column-description', data_file('adult', 'train.cd'),
        '--boosting-type', boosting_type,
        '-i', '10',
        '-w', '0.03',
        '-T', '4',
        '-m', output_model_path,
        ('-Y' if is_inverted else '-X'), '2/10',
        '--eval-file', output_eval_path,
    )
    fit_catboost_gpu(params)
    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


@pytest.mark.parametrize('is_inverted', [False, True], ids=['', 'inverted'])
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_cv_for_query(is_inverted, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    params = (
        '--use-best-model', 'false',
        '--loss-function', 'QueryRMSE',
        '-f', data_file('querywise', 'train'),
        '--column-description', data_file('querywise', 'train.cd'),
        '--boosting-type', boosting_type,
        '-i', '10',
        '-T', '4',
        '-m', output_model_path,
        ('-Y' if is_inverted else '-X'), '2/7',
        '--eval-file', output_eval_path,
    )
    fit_catboost_gpu(params)
    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


@pytest.mark.parametrize('is_inverted', [False, True], ids=['', 'inverted'])
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_cv_for_pairs(is_inverted, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    params = (
        '--use-best-model', 'false',
        '--loss-function', 'PairLogit',
        '-f', data_file('querywise', 'train'),
        '--column-description', data_file('querywise', 'train.cd'),
        '--learn-pairs', data_file('querywise', 'train.pairs'),
        '--boosting-type', boosting_type,
        '-i', '10',
        '-T', '4',
        '-m', output_model_path,
        ('-Y' if is_inverted else '-X'), '2/7',
        '--eval-file', output_eval_path,
    )
    fit_catboost_gpu(params)
    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_custom_priors(boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    test_file = data_file('adult', 'test_small')
    cd_file = data_file('adult', 'train.cd')
    params = (
        '--use-best-model', 'false',
        '--loss-function', 'Logloss',
        '-f', data_file('adult', 'train_small'),
        '-t', test_file,
        '--column-description', cd_file,
        '--boosting-type', boosting_type,
        '-i', '10',
        '-w', '0.03',
        '-T', '4',
        '-m', output_model_path,
        '--ctr', 'Borders:Prior=-2:Prior=0:Prior=8/3:Prior=1:Prior=-1:Prior=3,'
                 'FeatureFreq:Prior=0',
        '--per-feature-ctr', '4:Borders:Prior=0.444,FeatureFreq:Prior=0.444;'
                             '6:Borders:Prior=0.666,FeatureFreq:Prior=0.666;'
                             '8:Borders:Prior=-0.888:Prior=2/3,FeatureFreq:Prior=-0.888:Prior=0.888'
    )

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


CTR_TYPES = ['Borders', 'Buckets', 'FloatTargetMeanValue',
             'Borders,FloatTargetMeanValue', 'Buckets,Borders']


@pytest.mark.parametrize('ctr_type', CTR_TYPES)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_ctr_type(ctr_type, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    cd_file = data_file('adult_crossentropy', 'train.cd')
    test_file = data_file('adult_crossentropy', 'test_proba')
    params = (
        '--use-best-model', 'false',
        '--loss-function', 'RMSE',
        '-f', data_file('adult_crossentropy', 'train_proba'),
        '-t', test_file,
        '--column-description', cd_file,
        '--boosting-type', boosting_type,
        '-i', '3',
        '-T', '4',
        '-m', output_model_path,
        '--ctr', ctr_type
    )
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)
    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('loss_function', LOSS_FUNCTIONS)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_meta(loss_function, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    meta_path = 'meta.tsv'
    params = (
        '--use-best-model', 'false',
        '--loss-function', loss_function,
        '-f', data_file('adult', 'train_small'),
        '-t', data_file('adult', 'test_small'),
        '--column-description', data_file('adult', 'train.cd'),
        '--boosting-type', boosting_type,
        '-i', '10',
        '-T', '4',
        '-m', output_model_path,
        '--name', 'test experiment',
    )
    # meta_path is implicit output file
    fit_catboost_gpu(params, output_data={meta_path: meta_path})

    return [local_canonical_file(meta_path)]


def test_train_dir():
    output_model_path = 'model.bin'
    train_dir_path = 'trainDir'
    params = (
        '--use-best-model', 'false',
        '--loss-function', 'RMSE',
        '-f', data_file('adult', 'train_small'),
        '-t', data_file('adult', 'test_small'),
        '--column-description', data_file('adult', 'train.cd'),
        '-i', '10',
        '-T', '4',
        '-m', output_model_path,
        '--train-dir', train_dir_path,
    )
    fit_catboost_gpu(params, output_data={train_dir_path: train_dir_path, output_model_path: output_model_path})
    outputs = ['time_left.tsv', 'learn_error.tsv', 'test_error.tsv', 'meta.tsv', output_model_path]
    for output in outputs:
        assert os.path.isfile(train_dir_path + '/' + output)


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
@pytest.mark.parametrize('qwise_loss', ['QueryRMSE', 'RMSE'])
def test_train_on_binarized_equal_train_on_float(boosting_type, qwise_loss):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_model_path_binarized = yatest.common.test_output_path('model_binarized.bin')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')

    borders_file = yatest.common.test_output_path('borders.tsv')
    borders_file_output = borders_file + '.out'
    predictions_path_learn = yatest.common.test_output_path('predictions_learn.tsv')
    predictions_path_learn_binarized = yatest.common.test_output_path('predictions_learn_binarized.tsv')
    predictions_path_test = yatest.common.test_output_path('predictions_test.tsv')
    predictions_path_test_binarized = yatest.common.test_output_path('predictions_test_binarized.tsv')

    learn_file = data_file('querywise', 'train')
    cd_file = data_file('querywise', 'train.cd')
    test_file = data_file('querywise', 'test')
    params = {"--loss-function": qwise_loss,
              "-f": learn_file,
              "-t": test_file,
              '--column-description': cd_file,
              '--boosting-type': boosting_type,
              '-i': '100',
              '-T': '4',
              '-m': output_model_path,
              '--learn-err-log': learn_error_path,
              '--test-err-log': test_error_path,
              '--use-best-model': 'false',
              '--output-borders-file': borders_file_output,
              }

    params_binarized = dict(params)
    params_binarized['--input-borders-file'] = borders_file_output
    params_binarized['--output-borders-file'] = borders_file
    params_binarized['-m'] = output_model_path_binarized

    fit_catboost_gpu(params)
    apply_catboost(output_model_path, learn_file, cd_file, predictions_path_learn)
    apply_catboost(output_model_path, test_file, cd_file, predictions_path_test)

    # learn_error_path and test_error_path already exist after first fit_catboost_gpu() call
    # and would be automatically marked as input_data for YT operation,
    # which will lead to error, because input files are available only for reading.
    # That's why we explicitly drop files from input_data and implicitly add them to output_data.
    fit_catboost_gpu(params_binarized, input_data={learn_error_path: None, test_error_path: None})

    apply_catboost(output_model_path_binarized, learn_file, cd_file, predictions_path_learn_binarized)
    apply_catboost(output_model_path_binarized, test_file, cd_file, predictions_path_test_binarized)

    assert (filecmp.cmp(predictions_path_learn, predictions_path_learn_binarized))
    assert (filecmp.cmp(predictions_path_test, predictions_path_test_binarized))

    return [local_canonical_file(learn_error_path, diff_tool=diff_tool()),
            local_canonical_file(test_error_path, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_test, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_learn, diff_tool=diff_tool()),
            local_canonical_file(borders_file, diff_tool=diff_tool())]


FSTR_TYPES = ['FeatureImportance', 'InternalFeatureImportance', 'InternalInteraction', 'Interaction', 'ShapValues']


@pytest.mark.parametrize('fstr_type', FSTR_TYPES)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_fstr(fstr_type, boosting_type):
    model_path = yatest.common.test_output_path('adult_model.bin')
    output_fstr_path = yatest.common.test_output_path('fstr.tsv')

    fit_params = (
        '--use-best-model', 'false',
        '--loss-function', 'Logloss',
        '-f', data_file('adult', 'train_small'),
        '--column-description', data_file('adult', 'train.cd'),
        '--boosting-type', boosting_type,
        '-i', '10',
        '-w', '0.03',
        '-T', '4',
        '--one-hot-max-size', '10',
        '-m', model_path
    )

    if fstr_type == 'ShapValues':
        fit_params += ('--max-ctr-complexity', '1')

    fit_catboost_gpu(fit_params)

    fstr_params = (
        '--input-path', data_file('adult', 'train_small'),
        '--column-description', data_file('adult', 'train.cd'),
        '-m', model_path,
        '-o', output_fstr_path,
        '--fstr-type', fstr_type
    )
    fstr_catboost_cpu(fstr_params)

    return local_canonical_file(output_fstr_path)


LOSS_FUNCTIONS_NO_MAPE = ['RMSE', 'Logloss', 'MAE', 'CrossEntropy', 'Quantile', 'LogLinQuantile', 'Poisson']


@pytest.mark.parametrize('loss_function', LOSS_FUNCTIONS_NO_MAPE)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_quantized_pool(loss_function, boosting_type):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    quantized_train_file = 'quantized://' + data_file('quantized_adult', 'train.qbin')
    quantized_test_file = 'quantized://' + data_file('quantized_adult', 'test.qbin')
    params = (
        '--use-best-model', 'false',
        '--loss-function', loss_function,
        '-f', quantized_train_file,
        '-t', quantized_test_file,
        '--boosting-type', boosting_type,
        '-i', '10',
        '-w', '0.03',
        '-T', '4',
        '-m', output_model_path,
    )

    fit_catboost_gpu(params)
    cd_file = data_file('quantized_adult', 'pool.cd')
    test_file = data_file('quantized_adult', 'test_small.tsv')
    apply_catboost(output_model_path, test_file, cd_file, output_eval_path)

    return [local_canonical_file(output_eval_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
@pytest.mark.parametrize('used_ram_limit', ['1Kb', '550Mb'])
def test_allow_writing_files_and_used_ram_limit(boosting_type, used_ram_limit):
    output_model_path = yatest.common.test_output_path('model.bin')
    output_eval_path = yatest.common.test_output_path('test.eval')

    cd_file = data_file('airlines_5K', 'cd')

    params = (
        '--use-best-model', 'false',
        '--allow-writing-files', 'false',
        '--used-ram-limit', used_ram_limit,
        '--loss-function', 'Logloss',
        '--max-ctr-complexity', '8',
        '--depth', '10',
        '-f', data_file('airlines_5K', 'train'),
        '-t', data_file('airlines_5K', 'test'),
        '--column-description', cd_file,
        '--has-header',
        '--boosting-type', boosting_type,
        '-i', '20',
        '-w', '0.03',
        '-T', '4',
        '-m', output_model_path,
        '--eval-file', output_eval_path,
    )
    fit_catboost_gpu(params)

    test_file = data_file('airlines_5K', 'test')
    apply_catboost(output_model_path, test_file, cd_file,
                   output_eval_path, has_header=True)

    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]


def test_pairs_generation():
    output_model_path = yatest.common.test_output_path('model.bin')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    predictions_path_learn = yatest.common.test_output_path('predictions_learn.tsv')
    predictions_path_test = yatest.common.test_output_path('predictions_test.tsv')

    cd_file = data_file('querywise', 'train.cd')
    learn_file = data_file('querywise', 'train')
    test_file = data_file('querywise', 'test')

    params = [
        '--loss-function', 'PairLogit',
        '--eval-metric', 'PairAccuracy',
        '-f', learn_file,
        '-t', test_file,
        '--column-description', cd_file,
        '--l2-leaf-reg', '0',
        '-i', '20',
        '-T', '4',
        '-m', output_model_path,
        '--learn-err-log', learn_error_path,
        '--test-err-log', test_error_path,
        '--use-best-model', 'false'
    ]
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, learn_file, cd_file, predictions_path_learn)
    apply_catboost(output_model_path, test_file, cd_file, predictions_path_test)

    return [local_canonical_file(learn_error_path, diff_tool=diff_tool()),
            local_canonical_file(test_error_path, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_learn, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_test, diff_tool=diff_tool()),
            ]


def test_pairs_generation_with_max_pairs():
    output_model_path = yatest.common.test_output_path('model.bin')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    predictions_path_learn = yatest.common.test_output_path('predictions_learn.tsv')
    predictions_path_test = yatest.common.test_output_path('predictions_test.tsv')

    cd_file = data_file('querywise', 'train.cd')
    learn_file = data_file('querywise', 'train')
    test_file = data_file('querywise', 'test')

    params = [
        '--loss-function', 'PairLogit:max_pairs=30',
        '--eval-metric', 'PairAccuracy',
        '-f', learn_file,
        '-t', test_file,
        '--column-description', cd_file,
        '--l2-leaf-reg', '0',
        '-i', '20',
        '-T', '4',
        '-m', output_model_path,
        '--learn-err-log', learn_error_path,
        '--test-err-log', test_error_path,
        '--use-best-model', 'false'
    ]
    fit_catboost_gpu(params)
    apply_catboost(output_model_path, learn_file, cd_file, predictions_path_learn)
    apply_catboost(output_model_path, test_file, cd_file, predictions_path_test)

    return [local_canonical_file(learn_error_path, diff_tool=diff_tool()),
            local_canonical_file(test_error_path, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_learn, diff_tool=diff_tool()),
            local_canonical_file(predictions_path_test, diff_tool=diff_tool()),
            ]


@pytest.mark.parametrize('task_type', ['CPU', 'GPU'])
def test_learn_without_header_eval_with_header(task_type):
    train_path = yatest.common.test_output_path('airlines_without_header')
    with open(data_file('airlines_5K', 'train'), 'r') as with_header_file:
        with open(train_path, 'w') as without_header_file:
            without_header_file.writelines(with_header_file.readlines()[1:])

    model_path = yatest.common.test_output_path('model.bin')

    fit_params = [
        '--loss-function', 'Logloss',
        '-f', train_path,
        '--cd', data_file('airlines_5K', 'cd'),
        '-i', '10',
        '-m', model_path
    ]
    execute_catboost_fit(
        task_type=task_type,
        params=fit_params,
        devices='0'
    )

    cmd_calc = (
        CATBOOST_PATH,
        'calc',
        '--input-path', data_file('airlines_5K', 'test'),
        '--cd', data_file('airlines_5K', 'cd'),
        '-m', model_path,
        '--has-header'
    )
    yatest.common.execute(cmd_calc)


def test_group_weights_file():
    first_eval_path = yatest.common.test_output_path('first.eval')
    second_eval_path = yatest.common.test_output_path('second.eval')
    first_model_path = yatest.common.test_output_path('first_model.bin')
    second_model_path = yatest.common.test_output_path('second_model.bin')

    def run_catboost(eval_path, model_path, cd_file, is_additional_query_weights):
        cd_file_path = data_file('querywise', cd_file)
        fit_params = [
            '--use-best-model', 'false',
            '--loss-function', 'QueryRMSE',
            '-f', data_file('querywise', 'train'),
            '--column-description', cd_file_path,
            '-i', '5',
            '-T', '4',
            '-m', model_path,
        ]
        if is_additional_query_weights:
            fit_params += [
                '--learn-group-weights', data_file('querywise', 'train.group_weights'),
                '--test-group-weights', data_file('querywise', 'test.group_weights'),
            ]
        fit_catboost_gpu(fit_params)
        apply_catboost(model_path, data_file('querywise', 'test'), cd_file_path, eval_path)

    run_catboost(first_eval_path, first_model_path, 'train.cd', True)
    run_catboost(second_eval_path, second_model_path, 'train.cd.group_weight', False)
    assert filecmp.cmp(first_eval_path, second_eval_path)

    return [local_canonical_file(first_eval_path)]


def test_group_weights_file_quantized():
    first_eval_path = yatest.common.test_output_path('first.eval')
    second_eval_path = yatest.common.test_output_path('second.eval')
    first_model_path = yatest.common.test_output_path('first_model.bin')
    second_model_path = yatest.common.test_output_path('second_model.bin')

    def run_catboost(eval_path, model_path, train, is_additional_query_weights):
        fit_params = [
            '--use-best-model', 'false',
            '--loss-function', 'QueryRMSE',
            '-f', 'quantized://' + data_file('querywise', train),
            '-i', '5',
            '-T', '4',
            '-m', model_path,
        ]
        if is_additional_query_weights:
            fit_params += [
                '--learn-group-weights', data_file('querywise', 'train.group_weights'),
                '--test-group-weights', data_file('querywise', 'test.group_weights'),
            ]
        fit_catboost_gpu(fit_params)
        apply_catboost(model_path, data_file('querywise', 'test'), data_file('querywise', 'train.cd.group_weight'), eval_path)

    run_catboost(first_eval_path, first_model_path, 'train.quantized', True)
    run_catboost(second_eval_path, second_model_path, 'train.quantized.group_weight', False)
    assert filecmp.cmp(first_eval_path, second_eval_path)

    return [local_canonical_file(first_eval_path)]


NO_RANDOM_PARAMS = {
    '--random-strength': '0',
    '--bootstrap-type': 'No',
    '--has-time': '',
    '--set-metadata-from-freeargs': ''
}

METRIC_CHECKING_MULTICLASS = 'Accuracy:use_weights=false'

CAT_COMPARE_PARAMS = {
    '--counter-calc-method': 'SkipTest',
    '--simple-ctr': 'Buckets',
    '--max-ctr-complexity': 1
}


def eval_metric(model_path, metrics, data_path, cd_path, output_log, eval_period='1'):
    cmd = [
        CATBOOST_PATH,
        'eval-metrics',
        '--metrics', metrics,
        '-m', model_path,
        '--input-path', data_path,
        '--cd', cd_path,
        '--output-path', output_log,
        '--eval-period', eval_period
    ]

    yatest.common.execute(cmd)


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
def test_class_weight_multiclass(loss_function):
    model_path = yatest.common.test_output_path('model.bin')

    test_error_path = yatest.common.test_output_path('test_error.tsv')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('adult', 'train_small')
    test_path = data_file('adult', 'test_small')
    cd_path = data_file('adult', 'train.cd')

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': loss_function,
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--class-weights': '0.5,2',
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS
    }

    fit_params.update(CAT_COMPARE_PARAMS)

    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)
    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)

    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('leaf_estimation_method', LEAF_ESTIMATION_METHOD)
def test_multi_leaf_estimation_method(leaf_estimation_method):
    output_model_path = yatest.common.test_output_path('model.bin')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_test_error_path = yatest.common.test_output_path('eval_test_error.tsv')

    train_path = data_file('cloudness_small', 'train_small')
    test_path = data_file('cloudness_small', 'test_small')
    cd_path = data_file('cloudness_small', 'train.cd')

    fit_params = {
        '--loss-function': 'MultiClass',
        '-f': train_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': output_model_path,
        '--leaf-estimation-method': leaf_estimation_method,
        '--leaf-estimation-iterations': '2',
        '--use-best-model': 'false',
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(output_model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_test_error_path)
    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_test_error_path)

    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
def test_multiclass_baseline(loss_function):
    labels = [0, 1, 2, 3]

    cd_path = yatest.common.test_output_path('cd.txt')
    np.savetxt(cd_path, [[0, 'Target'], [1, 'Baseline'], [2, 'Baseline'], [3, 'Baseline'], [4, 'Baseline']], fmt='%s', delimiter='\t')

    prng = np.random.RandomState(seed=0)

    train_path = yatest.common.test_output_path('train.txt')
    np.savetxt(train_path, generate_random_labeled_set(100, 1000, labels, prng=prng), fmt='%s', delimiter='\t')

    test_path = yatest.common.test_output_path('test.txt')
    np.savetxt(test_path, generate_random_labeled_set(100, 1000, labels, prng=prng), fmt='%s', delimiter='\t')

    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    fit_params = {
        '--loss-function': loss_function,
        '-f': train_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '--use-best-model': 'false',
        '--classes-count': '4',
        '--custom-metric': METRIC_CHECKING_MULTICLASS,
        '--test-err-log': eval_error_path
    }

    fit_params.update(NO_RANDOM_PARAMS)

    execute_catboost_fit('CPU', fit_params)

    fit_params['--learn-err-log'] = learn_error_path
    fit_params['--test-err-log'] = test_error_path
    fit_catboost_gpu(fit_params)

    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)
    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
def test_multiclass_baseline_lost_class(loss_function):
    num_objects = 1000

    cd_path = yatest.common.test_output_path('cd.txt')
    np.savetxt(cd_path, [[0, 'Target'], [1, 'Baseline'], [2, 'Baseline']], fmt='%s', delimiter='\t')

    prng = np.random.RandomState(seed=0)

    train_path = yatest.common.test_output_path('train.txt')
    np.savetxt(train_path, generate_random_labeled_set(num_objects, 10, labels=[1, 2], prng=prng), fmt='%.5f', delimiter='\t')

    test_path = yatest.common.test_output_path('test.txt')
    np.savetxt(test_path, generate_random_labeled_set(num_objects, 10, labels=[0, 1, 2, 3], prng=prng), fmt='%.5f', delimiter='\t')

    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    custom_metric = 'Accuracy:use_weights=false'

    fit_params = {
        '--loss-function': loss_function,
        '-f': train_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '--custom-metric': custom_metric,
        '--test-err-log': eval_error_path,
        '--use-best-model': 'false',
        '--classes-count': '4'
    }

    fit_params.update(NO_RANDOM_PARAMS)

    with pytest.raises(yatest.common.ExecutionError):
        execute_catboost_fit('CPU', fit_params)


def test_ctr_buckets():
    model_path = yatest.common.test_output_path('model.bin')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('adult', 'train_small')
    test_path = data_file('adult', 'test_small')
    cd_path = data_file('adult', 'train.cd')

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': 'MultiClass',
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS
    }

    fit_params.update(CAT_COMPARE_PARAMS)

    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)

    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)
    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
def test_multi_targets(loss_function):
    model_path = yatest.common.test_output_path('model.bin')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('cloudness_small', 'train_small')
    test_path = data_file('cloudness_small', 'test_small')
    cd_path = data_file('cloudness_small', 'train.cd')

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': loss_function,
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)

    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)
    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


def test_custom_loss_for_multiclassification():
    model_path = yatest.common.test_output_path('model.bin')

    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('cloudness_small', 'train_small')
    test_path = data_file('cloudness_small', 'test_small')
    cd_path = data_file('cloudness_small', 'train.cd')

    custom_metric = [
        'Accuracy',
        'Precision',
        'Recall',
        'F1',
        'TotalF1',
        'MCC',
        'Kappa',
        'WKappa',
        'ZeroOneLoss',
        'HammingLoss',
        'HingeLoss'
    ]

    custom_metric_string = ','.join(custom_metric)

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': 'MultiClass',
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--custom-metric': custom_metric_string,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(model_path, custom_metric_string, test_path, cd_path, eval_error_path)
    compare_evals(custom_metric, test_error_path, eval_error_path)

    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_custom_loss_for_classification(boosting_type):
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    model_path = yatest.common.test_output_path('model.bin')

    learn_path = data_file('adult', 'train_small')
    test_path = data_file('adult', 'test_small')
    cd_path = data_file('adult', 'train.cd')

    custom_metric = [
        'AUC',
        'CrossEntropy',
        'Accuracy',
        'Precision',
        'Recall',
        'F1',
        'TotalF1',
        'MCC',
        'BalancedAccuracy',
        'BalancedErrorRate',
        'Kappa',
        'WKappa',
        'BrierScore',
        'ZeroOneLoss',
        'HammingLoss',
        'HingeLoss'
    ]

    custom_metric_string = ','.join(custom_metric)

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': 'Logloss',
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': boosting_type,
        '-w': '0.03',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--custom-metric': custom_metric_string,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path
    }

    fit_params.update(CAT_COMPARE_PARAMS)

    fit_catboost_gpu(fit_params)

    eval_metric(model_path, custom_metric_string, test_path, cd_path, eval_error_path)
    compare_evals(custom_metric, test_error_path, eval_error_path)

    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
def test_class_names_multiclass(loss_function):
    model_path = yatest.common.test_output_path('model.bin')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('precipitation_small', 'train_small')
    test_path = data_file('precipitation_small', 'test_small')
    cd_path = data_file('precipitation_small', 'train.cd')

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': loss_function,
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS,
        '--class-names': '0.,0.5,1.,0.25,0.75'
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)
    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)

    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
def test_lost_class(loss_function):
    model_path = yatest.common.test_output_path('model.bin')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('cloudness_lost_class', 'train_small')
    test_path = data_file('cloudness_lost_class', 'test_small')
    cd_path = data_file('cloudness_lost_class', 'train.cd')

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': loss_function,
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--classes-count': '3'
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)
    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)

    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


def test_class_weight_with_lost_class():
    model_path = yatest.common.test_output_path('model.bin')
    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    learn_path = data_file('cloudness_lost_class', 'train_small')
    test_path = data_file('cloudness_lost_class', 'test_small')
    cd_path = data_file('cloudness_lost_class', 'train.cd')

    fit_params = {
        '--use-best-model': 'false',
        '--loss-function': 'MultiClass',
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '--boosting-type': 'Plain',
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--classes-count': '3',
        '--class-weights': '0.5,2,2',
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--custom-metric': METRIC_CHECKING_MULTICLASS
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)
    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)

    return [local_canonical_file(eval_error_path)]


@pytest.mark.parametrize('metric_period', ['1', '2'])
@pytest.mark.parametrize('metric', ['MultiClass', 'MultiClassOneVsAll', 'F1', 'Accuracy', 'TotalF1', 'MCC', 'Precision', 'Recall'])
@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
@pytest.mark.parametrize('dataset', ['cloudness_small', 'cloudness_lost_class'])
def test_eval_metrics_multiclass(metric, loss_function, dataset, metric_period):
    if loss_function == 'MultiClass' and metric == 'MultiClassOneVsAll' or loss_function == 'MultiClassOneVsAll' and metric == 'MultiClass':
        return

    learn_path = data_file(dataset, 'train_small')
    test_path = data_file(dataset, 'test_small')
    cd_path = data_file(dataset, 'train.cd')

    model_path = yatest.common.test_output_path('model.bin')

    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    fit_params = {
        '--loss-function': loss_function,
        '--custom-metric': metric,
        '--boosting-type': 'Plain',
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--use-best-model': 'false',
        '--classes-count': '3',
        '--metric-period': metric_period
    }

    fit_params.update(CAT_COMPARE_PARAMS)
    fit_catboost_gpu(fit_params)

    eval_metric(model_path, metric, test_path, cd_path, eval_error_path, metric_period)

    idx_test_metric = 1 if metric == loss_function else 2

    first_metrics = np.round(np.loadtxt(test_error_path, skiprows=1)[:, idx_test_metric], 5)
    second_metrics = np.round(np.loadtxt(eval_error_path, skiprows=1)[:, 1], 5)
    assert np.all(first_metrics == second_metrics)
    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


def test_eval_metrics_class_names():
    labels = ['a', 'b', 'c', 'd']
    model_path = yatest.common.test_output_path('model.bin')

    cd_path = yatest.common.test_output_path('cd.txt')
    np.savetxt(cd_path, [[0, 'Target']], fmt='%s', delimiter='\t')

    prng = np.random.RandomState(seed=0)

    train_path = yatest.common.test_output_path('train.txt')
    np.savetxt(train_path, generate_random_labeled_set(100, 10, labels, prng=prng), fmt='%s', delimiter='\t')

    test_path = yatest.common.test_output_path('test.txt')
    np.savetxt(test_path, generate_random_labeled_set(100, 10, labels, prng=prng), fmt='%s', delimiter='\t')

    learn_error_path = yatest.common.test_output_path('learn_error.tsv')
    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    custom_metric = 'TotalF1,MultiClass'

    fit_params = {
        '--loss-function': 'MultiClass',
        '--custom-metric': custom_metric,
        '--boosting-type': 'Plain',
        '-f': train_path,
        '-t': test_path,
        '--column-description': cd_path,
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--learn-err-log': learn_error_path,
        '--test-err-log': test_error_path,
        '--use-best-model': 'false',
        '--class-names': ','.join(labels)
    }

    fit_catboost_gpu(fit_params)

    eval_metric(model_path, custom_metric, test_path, cd_path, eval_error_path)

    first_metrics = np.round(np.loadtxt(test_error_path, skiprows=1)[:, 2], 5)
    second_metrics = np.round(np.loadtxt(eval_error_path, skiprows=1)[:, 1], 5)
    assert np.all(first_metrics == second_metrics)
    return [local_canonical_file(learn_error_path), local_canonical_file(test_error_path)]


def test_fit_multiclass_with_class_names():
    labels = ['a', 'b', 'c', 'd']

    model_path = yatest.common.test_output_path('model.bin')

    cd_path = yatest.common.test_output_path('cd.txt')
    np.savetxt(cd_path, [[0, 'Target']], fmt='%s', delimiter='\t')

    prng = np.random.RandomState(seed=0)

    learn_path = yatest.common.test_output_path('train.txt')
    np.savetxt(learn_path, generate_random_labeled_set(100, 10, labels, prng=prng), fmt='%s', delimiter='\t')

    test_path = yatest.common.test_output_path('test.txt')
    np.savetxt(test_path, generate_random_labeled_set(100, 10, labels, prng=prng), fmt='%s', delimiter='\t')

    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    fit_params = {
        '--loss-function': 'MultiClass',
        '--boosting-type': 'Plain',
        '--custom-metric': METRIC_CHECKING_MULTICLASS,
        '--class-names': ','.join(labels),
        '-f': learn_path,
        '-t': test_path,
        '--column-description': cd_path,
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--use-best-model': 'false',
        '--test-err-log': test_error_path
    }

    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)

    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)

    return [local_canonical_file(test_error_path)]


def test_extract_multiclass_labels_from_class_names():
    labels = ['a', 'b', 'c', 'd']

    model_path = yatest.common.test_output_path('model.bin')

    cd_path = yatest.common.test_output_path('cd.txt')
    np.savetxt(cd_path, [[0, 'Target']], fmt='%s', delimiter='\t')

    prng = np.random.RandomState(seed=0)

    train_path = yatest.common.test_output_path('train.txt')
    np.savetxt(train_path, generate_random_labeled_set(100, 10, labels, prng=prng), fmt='%s', delimiter='\t')

    test_path = yatest.common.test_output_path('test.txt')
    np.savetxt(test_path, generate_random_labeled_set(100, 10, labels, prng=prng), fmt='%s', delimiter='\t')

    test_error_path = yatest.common.test_output_path('test_error.tsv')
    eval_error_path = yatest.common.test_output_path('eval_error.tsv')

    fit_params = {
        '--loss-function': 'MultiClass',
        '--class-names': ','.join(labels),
        '--boosting-type': 'Plain',
        '--custom-metric': METRIC_CHECKING_MULTICLASS,
        '-f': train_path,
        '-t': test_path,
        '--column-description': cd_path,
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--use-best-model': 'false',
        '--test-err-log': test_error_path
    }

    fit_catboost_gpu(fit_params)

    eval_metric(model_path, METRIC_CHECKING_MULTICLASS, test_path, cd_path, eval_error_path)
    compare_evals(METRIC_CHECKING_MULTICLASS, test_error_path, eval_error_path)

    py_catboost = catboost.CatBoost()
    py_catboost.load_model(model_path)

    assert json.loads(py_catboost.get_metadata()['multiclass_params'])['class_to_label'] == [0, 1, 2, 3]
    assert json.loads(py_catboost.get_metadata()['multiclass_params'])['class_names'] == ['a', 'b', 'c', 'd']
    assert json.loads(py_catboost.get_metadata()['multiclass_params'])['classes_count'] == 0

    assert json.loads(py_catboost.get_metadata()['params'])['data_processing_options']['class_names'] == ['a', 'b', 'c', 'd']

    return [local_canonical_file(test_error_path)]


@pytest.mark.parametrize('loss_function', MULTICLASS_LOSSES)
@pytest.mark.parametrize('prediction_type', ['Probability', 'RawFormulaVal', 'Class'])
def test_save_and_apply_multiclass_labels_from_classes_count(loss_function, prediction_type):
    model_path = yatest.common.test_output_path('model.bin')

    cd_path = yatest.common.test_output_path('cd.txt')
    np.savetxt(cd_path, [[0, 'Target']], fmt='%s', delimiter='\t')

    prng = np.random.RandomState(seed=0)

    train_path = yatest.common.test_output_path('train.txt')
    np.savetxt(train_path, generate_random_labeled_set(100, 10, [1, 2], prng=prng), fmt='%s', delimiter='\t')

    test_path = yatest.common.test_output_path('test.txt')
    np.savetxt(test_path, generate_random_labeled_set(100, 10, [0, 1, 2, 3], prng=prng), fmt='%s', delimiter='\t')

    eval_path = yatest.common.test_output_path('eval.txt')

    fit_params = {
        '--loss-function': loss_function,
        '--boosting-type': 'Plain',
        '--classes-count': '4',
        '-f': train_path,
        '--column-description': cd_path,
        '-i': '10',
        '-T': '4',
        '-m': model_path,
        '--use-best-model': 'false'
    }

    fit_catboost_gpu(fit_params)

    py_catboost = catboost.CatBoost()
    py_catboost.load_model(model_path)

    assert json.loads(py_catboost.get_metadata()['multiclass_params'])['class_to_label'] == [1, 2]
    assert json.loads(py_catboost.get_metadata()['multiclass_params'])['classes_count'] == 4
    assert json.loads(py_catboost.get_metadata()['multiclass_params'])['class_names'] == []

    calc_cmd = (
        CATBOOST_PATH,
        'calc',
        '--input-path', test_path,
        '--column-description', cd_path,
        '-m', model_path,
        '--output-path', eval_path,
        '--prediction-type', prediction_type
    )

    yatest.common.execute(calc_cmd)

    if prediction_type == 'RawFormulaVal':
        with open(eval_path, "rt") as f:
            for i, line in enumerate(f):
                if i == 0:
                    assert line[:-1] == 'DocId\t{}:Class=0\t{}:Class=1\t{}:Class=2\t{}:Class=3' \
                        .format(prediction_type, prediction_type, prediction_type, prediction_type)
                else:
                    assert float(line[:-1].split()[1]) == float('-inf') and float(line[:-1].split()[4]) == float('-inf')  # fictitious approxes must be negative infinity

    if prediction_type == 'Probability':
        with open(eval_path, "rt") as f:
            for i, line in enumerate(f):
                if i == 0:
                    assert line[:-1] == 'DocId\t{}:Class=0\t{}:Class=1\t{}:Class=2\t{}:Class=3' \
                        .format(prediction_type, prediction_type, prediction_type, prediction_type)
                else:
                    assert abs(float(line[:-1].split()[1])) < 1e-307 \
                        and abs(float(line[:-1].split()[4])) < 1e-307  # fictitious probabilities must be virtually zero

    if prediction_type == 'Class':
        with open(eval_path, "rt") as f:
            for i, line in enumerate(f):
                if i == 0:
                    assert line[:-1] == 'DocId\tClass'
                else:
                    assert float(line[:-1].split()[1]) in [1, 2]  # probability of 0,3 classes appearance must be zero

    return [local_canonical_file(eval_path)]


REG_LOSS_FUNCTIONS = ['RMSE', 'MAE', 'Lq:q=1', 'Lq:q=1.5', 'Lq:q=3']
CUSTOM_METRIC = ["MAE,Lq:q=2.5,NumErrors:greater_than=0.1,NumErrors:greater_than=0.01,NumErrors:greater_than=0.5"]


@pytest.mark.parametrize('loss_function', REG_LOSS_FUNCTIONS)
@pytest.mark.parametrize('custom_metric', CUSTOM_METRIC)
@pytest.mark.parametrize('boosting_type', BOOSTING_TYPE)
def test_reg_targets(loss_function, boosting_type, custom_metric):
    test_error_path = yatest.common.test_output_path("test_error.tsv")
    params = [
        '--use-best-model', 'false',
        '--loss-function', loss_function,
        '-f', data_file('adult_crossentropy', 'train_proba'),
        '-t', data_file('adult_crossentropy', 'test_proba'),
        '--column-description', data_file('adult_crossentropy', 'train.cd'),
        '-i', '10',
        '-T', '4',
        '--counter-calc-method', 'SkipTest',
        '--custom-metric', custom_metric,
        '--test-err-log', test_error_path,
        '--boosting-type', boosting_type
    ]
    fit_catboost_gpu(params)

    return [local_canonical_file(test_error_path, diff_tool=diff_tool(1e-5))]


def test_eval_result_on_different_pool_type():
    output_eval_path = yatest.common.test_output_path('test.eval')
    output_quantized_eval_path = yatest.common.test_output_path('test.eval.quantized')

    def get_params(train, test, eval_path):
        return (
            '--use-best-model', 'false',
            '--loss-function', 'Logloss',
            '-f', train,
            '-t', test,
            '--cd', data_file('querywise', 'train.cd'),
            '-i', '10',
            '-T', '4',
            '--eval-file', eval_path,
        )

    def get_pool_path(set_name, is_quantized=False):
        path = data_file('querywise', set_name)
        return 'quantized://' + path + '.quantized' if is_quantized else path

    fit_catboost_gpu(get_params(get_pool_path('train'), get_pool_path('test'), output_eval_path))
    fit_catboost_gpu(get_params(get_pool_path('train', True), get_pool_path('test', True), output_quantized_eval_path))

    assert filecmp.cmp(output_eval_path, output_quantized_eval_path)
    return [local_canonical_file(output_eval_path)]


def compare_evals_with_precision(fit_eval, calc_eval):
    array_fit = np.genfromtxt(fit_eval, delimiter='\t', skip_header=True)
    array_calc = np.genfromtxt(calc_eval, delimiter='\t', skip_header=True)
    if open(fit_eval, "r").readline().split()[:-1] != open(calc_eval, "r").readline().split():
        return False
    array_fit = np.delete(array_fit, np.s_[-1], 1)
    return np.all(np.isclose(array_fit, array_calc, rtol=1e-6))


def test_convert_model_to_json_without_cat_features():
    output_model_path = yatest.common.test_output_path('model.json')
    output_eval_path = yatest.common.test_output_path('test.eval')
    fit_params = [
        '--use-best-model', 'false',
        '-f', data_file('higgs', 'train_small'),
        '-t', data_file('higgs', 'test_small'),
        '--column-description', data_file('higgs', 'train.cd'),
        '-i', '20',
        '-T', '4',
        '-r', '0',
        '--eval-file', output_eval_path,
        '-m', output_model_path,
        '--model-format', 'Json'
    ]
    fit_catboost_gpu(fit_params)

    formula_predict_path = yatest.common.test_output_path('predict_test.eval')
    calc_cmd = (
        CATBOOST_PATH,
        'calc',
        '--input-path', data_file('higgs', 'test_small'),
        '--column-description', data_file('higgs', 'train.cd'),
        '-m', output_model_path,
        '--model-format', 'Json',
        '--output-path', formula_predict_path
    )
    execute(calc_cmd)
    assert (compare_evals_with_precision(output_eval_path, formula_predict_path))
    return [local_canonical_file(output_eval_path, diff_tool=diff_tool())]
