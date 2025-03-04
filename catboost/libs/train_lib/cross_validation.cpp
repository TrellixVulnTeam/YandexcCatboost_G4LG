#include "cross_validation.h"

#include "approx_dimension.h"
#include "data.h"
#include "preprocess.h"
#include "train_model.h"

#include <catboost/libs/algo/calc_score_cache.h>
#include <catboost/libs/algo/helpers.h>
#include <catboost/libs/algo/learn_context.h>
#include <catboost/libs/algo/roc_curve.h>
#include <catboost/libs/algo/train.h>
#include <catboost/libs/helpers/exception.h>
#include <catboost/libs/helpers/restorable_rng.h>
#include <catboost/libs/helpers/vector_helpers.h>
#include <catboost/libs/loggers/catboost_logger_helpers.h>
#include <catboost/libs/loggers/logger.h>
#include <catboost/libs/logging/logging.h>
#include <catboost/libs/logging/profile_info.h>
#include <catboost/libs/metrics/metric.h>
#include <catboost/libs/model/features.h>
#include <catboost/libs/options/defaults_helper.h>
#include <catboost/libs/options/enum_helpers.h>
#include <catboost/libs/options/output_file_options.h>
#include <catboost/libs/options/plain_options_helper.h>

#include <util/folder/tempdir.h>
#include <util/generic/algorithm.h>
#include <util/generic/scope.h>
#include <util/generic/ymath.h>

#include <cmath>
#include <numeric>


using namespace NCB;


TConstArrayRef<TString> GetTargetForStratifiedSplit(const TDataProvider& dataProvider) {
    auto maybeTarget = dataProvider.RawTargetData.GetTarget();
    CB_ENSURE(maybeTarget, "Cannot do stratified split: Target data is unavailable");
    return *maybeTarget;
}


TConstArrayRef<float> GetTargetForStratifiedSplit(const TTrainingDataProvider& dataProvider) {
    return NCB::GetTarget(dataProvider.TargetData);
}


TVector<TArraySubsetIndexing<ui32>> CalcTrainSubsets(
    const TVector<TArraySubsetIndexing<ui32>>& testSubsets,
    ui32 groupCount
) {

    TVector<TVector<ui32>> trainSubsetIndices(testSubsets.size());
    for (ui32 fold = 0; fold < testSubsets.size(); ++fold) {
        trainSubsetIndices[fold].reserve(groupCount - testSubsets[fold].Size());
    }
    for (ui32 testFold = 0; testFold < testSubsets.size(); ++testFold) {
        testSubsets[testFold].ForEach(
            [&](ui32 /*idx*/, ui32 srcIdx) {
                for (ui32 fold = 0; fold < trainSubsetIndices.size(); ++fold) {
                    if (testFold == fold) {
                        continue;
                    }
                    trainSubsetIndices[fold].push_back(srcIdx);
                }
            }
        );
    }

    TVector<TArraySubsetIndexing<ui32>> result;
    for (auto& foldIndices : trainSubsetIndices) {
        result.push_back( TArraySubsetIndexing<ui32>(std::move(foldIndices)) );
    }

    return result;
}

static double ComputeStdDev(const TVector<double>& values, double avg) {
    double sqrSum = 0.0;
    for (double value : values) {
        sqrSum += Sqr(value - avg);
    }
    return std::sqrt(sqrSum / (values.size() - 1));
}

static TCVIterationResults ComputeIterationResults(
    const TVector<double>& trainErrors,
    const TVector<double>& testErrors,
    size_t foldCount
) {
    TCVIterationResults cvResults;
    cvResults.AverageTrain = Accumulate(trainErrors.begin(), trainErrors.end(), 0.0) / foldCount;
    cvResults.StdDevTrain = ComputeStdDev(trainErrors, cvResults.AverageTrain);
    cvResults.AverageTest = Accumulate(testErrors.begin(), testErrors.end(), 0.0) / foldCount;
    cvResults.StdDevTest = ComputeStdDev(testErrors, cvResults.AverageTest);
    return cvResults;
}


inline bool DivisibleOrLastIteration(int currentIteration, int iterationsCount, int period) {
    return currentIteration % period == 0 || currentIteration == iterationsCount - 1;
}



struct TFoldContext {
    TString NamesPrefix;

    THolder<TTempDir> TempDir; // THolder because of bugs with move semantics of TTempDir
    NCatboostOptions::TOutputFilesOptions OutputOptions; // with modified Overfitting params, TrainDir
    TTrainingDataProviders TrainingData;

    TVector<TVector<double>> MetricValuesOnTrain; // [iter][metricIdx]
    TVector<TVector<double>> MetricValuesOnTest;  // [iter][metricIdx]

    TEvalResult LastUpdateEvalResult;

    TRestorableFastRng64 Rand;

public:
    TFoldContext(
        size_t foldIdx,
        const NJson::TJsonValue& commonOutputJsonParams,
        TTrainingDataProviders&& trainingData,
        ui64 randomSeed)
        : NamesPrefix("fold_" + ToString(foldIdx) + "_")
        , TempDir(MakeHolder<TTempDir>())
        , TrainingData(std::move(trainingData))
        , Rand(randomSeed)
    {
        NJson::TJsonValue outputJsonParams = commonOutputJsonParams;
        outputJsonParams["train_dir"] = TempDir->Name();
        outputJsonParams["use_best_model"] = false;
        OutputOptions.Load(outputJsonParams);
    }

    void TrainUpToIteration(
        const NJson::TJsonValue& trainOptionsJson,
        const TMaybe<TCustomObjectiveDescriptor>& objectiveDescriptor,
        const TMaybe<TCustomMetricDescriptor>& evalMetricDescriptor,
        const TLabelConverter& labelConverter,
        TConstArrayRef<THolder<IMetric>> metrics,
        TConstArrayRef<bool> skipMetricOnTrain,
        size_t upToIteration, // exclusive bound
        size_t globalMaxIteration,
        bool isErrorTrackerActive,
        IModelTrainer* modelTrainer,
        NPar::TLocalExecutor* localExecutor) {

        TSetLoggingSilent silentMode;

        modelTrainer->TrainModel(
            true,
            trainOptionsJson,
            OutputOptions,
            objectiveDescriptor,
            evalMetricDescriptor,
            [&, this] (const TMetricsAndTimeLeftHistory& metricsAndTimeHistory) -> bool {
                Y_VERIFY(metricsAndTimeHistory.TimeHistory.size() > 0);
                size_t iteration = metricsAndTimeHistory.TimeHistory.size() - 1;

                // replay
                if (iteration < MetricValuesOnTest.size()) {
                    return true;
                }

                bool calcMetrics = DivisibleOrLastIteration(
                    iteration,
                    globalMaxIteration,
                    OutputOptions.GetMetricPeriod()
                );

                const bool calcErrorTrackerMetric = calcMetrics || isErrorTrackerActive;
                const int errorTrackerMetricIdx = calcErrorTrackerMetric ? 0 : -1;

                MetricValuesOnTrain.resize(iteration + 1);
                MetricValuesOnTest.resize(iteration + 1);

                for (auto metricIdx : xrange((int)metrics.size())) {
                    if (!calcMetrics && (metricIdx != errorTrackerMetricIdx)) {
                        continue;
                    }
                    const auto& metric = metrics[metricIdx];
                    const TString& metricDescription = metric->GetDescription();

                    MetricValuesOnTrain[iteration].push_back(
                        skipMetricOnTrain[metricIdx] ?
                        0.0 :
                        metricsAndTimeHistory.LearnMetricsHistory.back().at(metricDescription));

                    MetricValuesOnTest[iteration].push_back(
                        metricsAndTimeHistory.TestMetricsHistory.back()[0].at(metricDescription));
                }

                return (iteration + 1) < upToIteration;
            },
            TrainingData,
            labelConverter,
            localExecutor,
            /*rand*/ Nothing(),
            /*model*/ nullptr,
            TVector<TEvalResult*>{&LastUpdateEvalResult},
            /*metricsAndTimeHistory*/nullptr
        );
    }
};


static void DisableMetricSkipTrain(NJson::TJsonValue* metric) {
    NJson::TJsonValue& params = (*metric)["params"];
    TMap<TString, TString> hints;
    if (params.Has("hints")) {
        hints = ParseHintsDescription(params["hints"].GetStringSafe());
    }
    hints["skip_train"] = "false";
    params["hints"] = MakeHintsDescription(hints);

}

// TODO(akhropov): proper support - MLTOOLS-1863
static void DisableMetricsSkipTrain(NJson::TJsonValue* trainOptionsJson) {
    NJson::TJsonValue& metrics = (*trainOptionsJson)["metrics"];

    if (metrics.Has("eval_metric")) {
        DisableMetricSkipTrain(&metrics["eval_metric"]);
    }
    if (metrics.Has("custom_metrics")) {
        NJson::TJsonValue& customMetrics = metrics["custom_metrics"];
        for (auto& metricDescription : customMetrics.GetArraySafe()) {
            DisableMetricSkipTrain(&metricDescription);
        }
    }
}


static void UpdatePermutationBlockSize(
    ETaskType taskType,
    TConstArrayRef<TTrainingDataProviders> foldsData,
    NJson::TJsonValue* updatedTrainOptionsJson
) {
    if (taskType == ETaskType::GPU) {
        return;
    }

    bool isAnyFoldHasNonConsecutiveLearnFeaturesData = AnyOf(
        foldsData,
        [&] (const TTrainingDataProviders& foldData) {
            const auto& learnObjectsDataProvider
                = dynamic_cast<const TQuantizedForCPUObjectsDataProvider&>(*foldData.Learn->ObjectsData);

            return !learnObjectsDataProvider.GetFeaturesArraySubsetIndexing().IsConsecutive();
        }
    );

    if (isAnyFoldHasNonConsecutiveLearnFeaturesData) {
        (*updatedTrainOptionsJson)["boosting_options"]["fold_permutation_block"] = 1;
    }
}


void CrossValidate(
    const NJson::TJsonValue& plainJsonParams,
    const TMaybe<TCustomObjectiveDescriptor>& objectiveDescriptor,
    const TMaybe<TCustomMetricDescriptor>& evalMetricDescriptor,
    TDataProviderPtr data,
    const TCrossValidationParams& cvParams,
    TVector<TCVResult>* results
) {
    NJson::TJsonValue jsonParams;
    NJson::TJsonValue outputJsonParams;
    NCatboostOptions::PlainJsonToOptions(plainJsonParams, &jsonParams, &outputJsonParams);
    NCatboostOptions::TCatBoostOptions catBoostOptions(NCatboostOptions::LoadOptions(jsonParams));
    NCatboostOptions::TOutputFilesOptions outputFileOptions;
    outputFileOptions.Load(outputJsonParams);


    const ui32 allDataObjectCount = data->ObjectsData->GetObjectCount();

    CB_ENSURE(allDataObjectCount != 0, "Pool is empty");
    CB_ENSURE(allDataObjectCount > cvParams.FoldCount, "Pool is too small to be split into folds");

    // TODO(akhropov): implement ordered split. MLTOOLS-2486.
    CB_ENSURE(
        data->ObjectsData->GetOrder() != EObjectsOrder::Ordered,
        "Cross-validation for Ordered objects data is not yet implemented"
    );

    const ui32 oneFoldSize = allDataObjectCount / cvParams.FoldCount;
    const ui32 cvTrainSize = cvParams.Inverted ? oneFoldSize : oneFoldSize * (cvParams.FoldCount - 1);
    SetDataDependentDefaults(
        cvTrainSize,
        /*testPoolSize=*/allDataObjectCount - cvTrainSize,
        /*hasTestLabels=*/data->MetaInfo.HasTarget,
        /*hasTestPairs*/data->MetaInfo.HasPairs,
        &outputFileOptions.UseBestModel,
        &catBoostOptions
    );


    TRestorableFastRng64 rand(cvParams.PartitionRandSeed);

    NPar::TLocalExecutor localExecutor;
    localExecutor.RunAdditionalThreads(catBoostOptions.SystemOptions->NumThreads.Get() - 1);

    if (cvParams.Shuffle) {
        auto objectsGroupingSubset = NCB::Shuffle(data->ObjectsGrouping, 1, &rand);
        data = data->GetSubset(objectsGroupingSubset, &localExecutor);
    }

    TLabelConverter labelConverter;

    TTrainingDataProviderPtr trainingData = GetTrainingData(
        std::move(data),
        /*isLearnData*/ true,
        TStringBuf(),
        Nothing(), // TODO(akhropov): allow loading borders and nanModes in CV?
        /*unloadCatFeaturePerfectHashFromRamIfPossible*/ true,
        /*ensureConsecutiveLearnFeaturesDataForCpu*/ false,
        outputFileOptions.AllowWriteFiles(),
        /*quantizedFeaturesInfo*/ nullptr,
        &catBoostOptions,
        &labelConverter,
        &localExecutor,
        &rand);

    NJson::TJsonValue updatedTrainOptionsJson = jsonParams;
    UpdateUndefinedClassNames(catBoostOptions.DataProcessingOptions, &updatedTrainOptionsJson);

    // disable overfitting detector on folds training, it will work on average values
    updatedTrainOptionsJson["boosting_options"]["od_config"]["type"] = "Iter";
    updatedTrainOptionsJson["boosting_options"]["od_config"]["wait_iterations"] =
        catBoostOptions.BoostingOptions->IterationCount.Get();

    // internal training output shouldn't interfere with main stdout
    updatedTrainOptionsJson["logging_level"] = "Silent";


    // TODO(nikitxskv): Remove this hot-fix and make correct skip-metrics support in cv.
    DisableMetricsSkipTrain(&updatedTrainOptionsJson);


    const ETaskType taskType = catBoostOptions.GetTaskType();

    THolder<IModelTrainer> modelTrainerHolder;

    const bool isGpuDeviceType = taskType == ETaskType::GPU;
    if (isGpuDeviceType && TTrainerFactory::Has(ETaskType::GPU)) {
        modelTrainerHolder = TTrainerFactory::Construct(ETaskType::GPU);
    } else {
        CB_ENSURE(
            !isGpuDeviceType,
            "Can't load GPU learning library. "
            "Module was not compiled or driver  is incompatible with package. "
            "Please install latest NVDIA driver and check again");
        modelTrainerHolder = TTrainerFactory::Construct(ETaskType::CPU);
    }

    TSetLogging inThisScope(catBoostOptions.LoggingLevel);

    ui32 approxDimension = GetApproxDimension(catBoostOptions, labelConverter);


    TVector<THolder<IMetric>> metrics = CreateMetrics(
        catBoostOptions.LossFunctionDescription,
        catBoostOptions.MetricOptions,
        evalMetricDescriptor,
        approxDimension
    );
    CheckMetrics(metrics, catBoostOptions.LossFunctionDescription.Get().GetLossFunction());


    TVector<bool> skipMetricOnTrain;

    bool hasQuerywiseMetric = false;
    for (const auto& metric : metrics) {
        if (metric.Get()->GetErrorType() == EErrorType::QuerywiseError) {
            hasQuerywiseMetric = true;
        }

        metric->AddHint("skip_train", "false");
        skipMetricOnTrain.push_back(false);
    }
    if (hasQuerywiseMetric) {
        CB_ENSURE(!cvParams.Stratified, "Stratified split is incompatible with groupwise metrics");
    }


    TVector<TTrainingDataProviders> foldsData = PrepareCvFolds<TTrainingDataProviders>(
        std::move(trainingData),
        cvParams,
        Nothing(),
        /* oldCvStyleSplit */ false,
        &localExecutor);

    /* ensure that all folds have the same permutation block size because some of them might be consecutive
       and some might not
    */
    UpdatePermutationBlockSize(taskType, foldsData, &updatedTrainOptionsJson);

    TVector<TFoldContext> foldContexts;

    for (auto foldIdx : xrange((size_t)cvParams.FoldCount)) {
        foldContexts.emplace_back(
            foldIdx,
            outputJsonParams,
            std::move(foldsData[foldIdx]),
            catBoostOptions.RandomSeed);
    }


    EMetricBestValue bestValueType;
    float bestPossibleValue;
    metrics.front()->GetBestValue(&bestValueType, &bestPossibleValue);

    TErrorTracker errorTracker = CreateErrorTracker(
        catBoostOptions.BoostingOptions->OverfittingDetector,
        bestPossibleValue,
        bestValueType,
        /* hasTest */ true);

    results->reserve(metrics.size());
    for (const auto& metric : metrics) {
        TCVResult result;
        result.Metric = metric->GetDescription();
        results->push_back(result);
    }

    TLogger logger;
    TString learnToken = "learn";
    TString testToken = "test";

    if (outputFileOptions.AllowWriteFiles()) {
        // TODO(akhropov): compatibility name
        TString namesPrefix = "fold_0_";

        TOutputFiles outputFiles(outputFileOptions, namesPrefix);

        TVector<TString> learnSetNames, testSetNames;
        for (auto foldIdx : xrange(cvParams.FoldCount)) {
            learnSetNames.push_back("fold_" + ToString(foldIdx) + "_learn");
            testSetNames.push_back("fold_" + ToString(foldIdx) + "_test");
        }
        AddFileLoggers(
            /*detailedProfile*/false,
            outputFiles.LearnErrorLogFile,
            outputFiles.TestErrorLogFile,
            outputFiles.TimeLeftLogFile,
            outputFiles.JsonLogFile,
            outputFiles.ProfileLogFile,
            outputFileOptions.GetTrainDir(),
            GetJsonMeta(
                catBoostOptions.BoostingOptions->IterationCount.Get(),
                outputFileOptions.GetName(),
                GetConstPointers(metrics),
                learnSetNames,
                testSetNames,
                ELaunchMode::CV),
            outputFileOptions.GetMetricPeriod(),
            &logger
        );
    }

    AddConsoleLogger(
        learnToken,
        {testToken},
        /*hasTrain=*/true,
        outputFileOptions.GetVerbosePeriod(),
        catBoostOptions.BoostingOptions->IterationCount,
        &logger
    );

    ui32 globalMaxIteration = catBoostOptions.BoostingOptions->IterationCount;

    TProfileInfo profile(globalMaxIteration);

    ui32 iteration = 0;

    for (ui32 batchStartIteration = 0;
         !errorTracker.GetIsNeedStop() && (batchStartIteration < globalMaxIteration);
         batchStartIteration += cvParams.IterationsBatchSize)
    {
        profile.StartIterationBlock();

        ui32 batchEndIteration = Min(
            batchStartIteration + cvParams.IterationsBatchSize,
            globalMaxIteration);

        for (auto& foldContext : foldContexts) {
            foldContext.TrainUpToIteration(
                updatedTrainOptionsJson,
                objectiveDescriptor,
                evalMetricDescriptor,
                labelConverter,
                metrics,
                skipMetricOnTrain,
                batchEndIteration,
                globalMaxIteration,
                errorTracker.IsActive(),
                modelTrainerHolder.Get(),
                &localExecutor);
        }

        while (true) {
            bool calcMetrics = DivisibleOrLastIteration(
                iteration,
                catBoostOptions.BoostingOptions->IterationCount,
                outputFileOptions.GetMetricPeriod()
            );

            const bool calcErrorTrackerMetric = calcMetrics || errorTracker.IsActive();
            const int errorTrackerMetricIdx = calcErrorTrackerMetric ? 0 : -1;

            TOneInterationLogger oneIterLogger(logger);

            for (int metricIdx = 0; metricIdx < metrics.ysize(); ++metricIdx) {
                if (!calcMetrics && metricIdx != errorTrackerMetricIdx) {
                    continue;
                }
                const auto& metric = metrics[metricIdx];

                TVector<double> trainFoldsMetric; // [foldIdx]
                TVector<double> testFoldsMetric; // [foldIdx]
                for (const auto& foldContext : foldContexts) {
                    trainFoldsMetric.push_back(foldContext.MetricValuesOnTrain[iteration][metricIdx]);
                    if (!skipMetricOnTrain[metricIdx]) {
                        oneIterLogger.OutputMetric(
                            foldContext.NamesPrefix + learnToken,
                            TMetricEvalResult(metric->GetDescription(), trainFoldsMetric.back(), metricIdx == errorTrackerMetricIdx)
                        );
                    }
                    testFoldsMetric.push_back(foldContext.MetricValuesOnTest[iteration][metricIdx]);
                    oneIterLogger.OutputMetric(
                        foldContext.NamesPrefix + testToken,
                        TMetricEvalResult(metric->GetDescription(), testFoldsMetric.back(), metricIdx == errorTrackerMetricIdx)
                    );
                }

                TCVIterationResults cvResults = ComputeIterationResults(trainFoldsMetric, testFoldsMetric, cvParams.FoldCount);

                (*results)[metricIdx].AppendOneIterationResults(cvResults);

                if (metricIdx == errorTrackerMetricIdx) {
                    TVector<double> valuesToLog;
                    errorTracker.AddError(cvResults.AverageTest, iteration, &valuesToLog);
                }

                if (!skipMetricOnTrain[metricIdx]) {
                    oneIterLogger.OutputMetric(
                        learnToken,
                        TMetricEvalResult(metric->GetDescription(),
                        cvResults.AverageTrain,
                        metricIdx == errorTrackerMetricIdx));
                }
                oneIterLogger.OutputMetric(
                    testToken,
                    TMetricEvalResult(
                        metric->GetDescription(),
                        cvResults.AverageTest,
                        errorTracker.GetBestError(),
                        errorTracker.GetBestIteration(),
                        metricIdx == errorTrackerMetricIdx
                    )
                );
            }

            bool lastIterInBatch = false;
            if (errorTracker.GetIsNeedStop()) {
                CATBOOST_NOTICE_LOG << "Stopped by overfitting detector "
                    << " (" << errorTracker.GetOverfittingDetectorIterationsWait() << " iterations wait)" << Endl;
                lastIterInBatch = true;
            }
            ++iteration;
            if (iteration == batchEndIteration) {
                lastIterInBatch = true;
            }
            if (lastIterInBatch) {
                profile.FinishIterationBlock(iteration - batchStartIteration);
                oneIterLogger.OutputProfile(profile.GetProfileResults());
                break;
            }
        }
    }

    if (!outputFileOptions.GetRocOutputPath().empty()) {
        CB_ENSURE(
            catBoostOptions.LossFunctionDescription->GetLossFunction() == ELossFunction::Logloss,
            "For ROC curve loss function must be Logloss."
        );
        TVector<TVector<double>> allApproxes;
        TVector<TConstArrayRef<float>> labels;
        for (auto& foldContext : foldContexts) {
            allApproxes.push_back(std::move(foldContext.LastUpdateEvalResult.GetRawValuesRef()[0][0]));
            labels.push_back(GetTarget(foldContext.TrainingData.Test[0]->TargetData));
        }

        TRocCurve rocCurve(allApproxes, labels, catBoostOptions.SystemOptions.Get().NumThreads);
        rocCurve.OutputRocCurve(outputFileOptions.GetRocOutputPath());
    }
}
