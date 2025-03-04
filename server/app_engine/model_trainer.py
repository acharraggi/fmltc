# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

__author__ = "lizlooney@google.com (Liz Looney)"

# Python Standard Library
from datetime import datetime, timedelta, timezone
import json
import logging
import traceback

# Other Modules
import googleapiclient.discovery
from google.oauth2 import service_account

# My Modules
import action
import blob_storage
import cloud_secrets
import constants
import exceptions
import storage
import util

BUCKET = ('%s' % constants.PROJECT_ID)

IMAGE_URI = ('gcr.io/%s/object_detection:2021_11_25' % constants.PROJECT_ID)
PACKAGE_URI = ('gs://%s/static/training/object_detection-0.1.tar.gz' % BUCKET)

STARTING_MODELS = {
    'SSD MobileNet v2 320x320': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v2_320x320_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v2_320x320_coco17_tpu-8/checkpoint/ckpt-0',
        'tpu_batch_size': 512,
        'gpu_batch_size': 32,
        'num_warmup_steps': 2000,
    },
    'SSD MobileNet V2 FPNLite 320x320': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_320x320_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_320x320_coco17_tpu-8/checkpoint/ckpt-0',
        'tpu_batch_size': 128,
        'gpu_batch_size': 32,
        'num_warmup_steps': 1000,
    },
    'SSD MobileNet V1 FPN 640x640': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v1_fpn_640x640_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v1_fpn_640x640_coco17_tpu-8/checkpoint/ckpt-0',
        'tpu_batch_size': 64,
        'gpu_batch_size': 16,
        'num_warmup_steps': 2000,
    },
    'SSD MobileNet V2 FPNLite 640x640': {
        'pipeline_config': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_640x640_coco17_tpu-8/pipeline.config',
        'checkpoint': 'tf2/20200711/ssd_mobilenet_v2_fpnlite_640x640_coco17_tpu-8/checkpoint/ckpt-0',
        'tpu_batch_size': 128,
        'gpu_batch_size': 16,
        'num_warmup_steps': 1000,
    },
}

CHECKPOINT_EVERY_N = 100

def validate_starting_model(s):
    if s not in STARTING_MODELS:
        try:
            return storage.validate_uuid(s)
        except:
            message = "Error: '%s' is not a valid argument." % s
            logging.critical(message)
            raise exceptions.HttpErrorBadRequest(message)
    return s


def get_data_for_root_template(use_tpu):
    return {
        'starting_models': list(STARTING_MODELS.keys()),
        'min_training_steps': get_min_training_steps(use_tpu),
        'max_training_steps': get_max_training_steps(use_tpu),
        'default_training_steps': __get_default_training_steps(use_tpu),
        'batch_sizes': __get_batch_sizes(use_tpu),
        'checkpoint_every_n': CHECKPOINT_EVERY_N,
    }


def get_min_training_steps(use_tpu):
    if use_tpu:
        return 100
    return 200


def get_max_training_steps(use_tpu):
    if use_tpu:
        return 4000
    return 8000


def __get_default_training_steps(use_tpu):
    if use_tpu:
        return 2000
    return 3000


def __get_batch_sizes(use_tpu):
    batch_sizes = {}
    for name, starting_model_data in STARTING_MODELS.items():
        batch_sizes[name] = starting_model_data['tpu_batch_size'] if use_tpu else starting_model_data['gpu_batch_size']
    return batch_sizes


def __get_batch_size(use_tpu, original_starting_model, train_frame_count):
    # The following code matches the code in function getBatchSize in util.js.
    starting_model_data = STARTING_MODELS[original_starting_model]
    batch_size = starting_model_data['tpu_batch_size'] if use_tpu else starting_model_data['gpu_batch_size']
    while batch_size > train_frame_count and batch_size >= 2:
        batch_size /= 2
    return batch_size


def __get_num_warmup_steps(original_starting_model, num_training_steps):
    starting_model_data = STARTING_MODELS[original_starting_model]
    return min(starting_model_data['num_warmup_steps'], num_training_steps)


def start_training_model(team_uuid, description, dataset_uuid_list,
        starting_model, max_running_minutes, num_training_steps, create_time_ms, use_tpu):
    found_starting_model = starting_model in STARTING_MODELS
    if found_starting_model:
        starting_model_uuid = None
        starting_model_entity = None
        original_starting_model = starting_model
        user_visible_starting_model = starting_model
        starting_model_data = STARTING_MODELS[starting_model]
        config_template_blob_name = 'static/training/models/%s' % (
            starting_model_data['pipeline_config'])
        fine_tune_checkpoint = 'gs://%s/static/training/models/%s' % (
            BUCKET, starting_model_data['checkpoint'])
    else:
        # starting_model is the model_uuid of one of the user's own models.
        starting_model_uuid = starting_model
        # storage.retrieve_model_entity will raise HttpErrorNotFound
        # if the team_uuid/starting_model_uuid is not found
        starting_model_entity = storage.retrieve_model_entity(team_uuid, starting_model_uuid)
        if starting_model_entity['trained_checkpoint_path'] == '':
            message = 'Error: Trained checkpoint not found for model_uuid=%s.' % starting_model_uuid
            logging.critical(message)
            raise exceptions.HttpErrorNotFound(message)
        original_starting_model = starting_model_entity['original_starting_model']
        if original_starting_model not in STARTING_MODELS:
            message = 'Error: Original starting model is not longer supported: %s' % original_starting_model
            logging.critical(message)
            raise exceptions.HttpErrorNotFound(message)
        # user_visible_starting_model is the description field of the model entity.
        user_visible_starting_model = starting_model_entity['description']
        starting_model_data = STARTING_MODELS[original_starting_model]
        config_template_blob_name = 'static/training/models/%s' % (
            starting_model_data['pipeline_config'])
        fine_tune_checkpoint = starting_model_entity['trained_checkpoint_path']
        # Remove trailing .index if it's there.
        if fine_tune_checkpoint.endswith('.index'):
            fine_tune_checkpoint = fine_tune_checkpoint[:-6]

    dataset_entities = storage.retrieve_dataset_entities(team_uuid, dataset_uuid_list)
    if len(dataset_entities) != len(dataset_uuid_list):
        message = 'Error: One or more datasets not found for dataset_uuids=%s.' % str(dataset_uuid_list)
        logging.critical(message)
        raise exceptions.HttpErrorNotFound(message)

    previous_training_steps = 0
    dataset_uuids = []
    train_input_path = []
    eval_input_path = []
    train_frame_count = 0
    eval_frame_count = 0
    train_negative_frame_count = 0
    eval_negative_frame_count = 0
    train_dict_label_to_count = {}
    eval_dict_label_to_count = {}
    sorted_label_list = None
    label_map_path = None
    if starting_model_entity is not None:
        previous_training_steps = starting_model_entity['total_training_steps']
        dataset_uuids.extend(starting_model_entity['dataset_uuids'])
        train_input_path.extend(starting_model_entity['train_input_path'])
        eval_input_path.extend(starting_model_entity['eval_input_path'])
        train_frame_count += starting_model_entity['train_frame_count']
        eval_frame_count += starting_model_entity['eval_frame_count']
        train_negative_frame_count += starting_model_entity['train_negative_frame_count']
        eval_negative_frame_count += starting_model_entity['eval_negative_frame_count']
        util.extend_dict_label_to_count(train_dict_label_to_count, starting_model_entity['train_dict_label_to_count'])
        util.extend_dict_label_to_count(eval_dict_label_to_count, starting_model_entity['eval_dict_label_to_count'])
        sorted_label_list = starting_model_entity['sorted_label_list']
        label_map_path = starting_model_entity['label_map_path']

    for dataset_entity in dataset_entities:
        dataset_uuids.append(dataset_entity['dataset_uuid'])
        train_input_path.append(dataset_entity['train_input_path'])
        eval_input_path.append(dataset_entity['eval_input_path'])
        train_frame_count += dataset_entity['train_frame_count']
        eval_frame_count += dataset_entity['eval_frame_count']
        train_negative_frame_count += dataset_entity['train_negative_frame_count']
        eval_negative_frame_count += dataset_entity['eval_negative_frame_count']
        util.extend_dict_label_to_count(train_dict_label_to_count, dataset_entity['train_dict_label_to_count'])
        util.extend_dict_label_to_count(eval_dict_label_to_count, dataset_entity['eval_dict_label_to_count'])
        if sorted_label_list is None:
            sorted_label_list = dataset_entity['sorted_label_list']
            label_map_path = dataset_entity['label_map_path']
        elif sorted_label_list != dataset_entity['sorted_label_list']:
            message = "Error: The datasets contain different labels and cannot be used together."
            logging.critical(message)
            raise exceptions.HttpErrorBadRequest(message)

    batch_size = __get_batch_size(use_tpu, original_starting_model, train_frame_count)
    num_warmup_steps = __get_num_warmup_steps(original_starting_model, num_training_steps)

    # Create the pipeline.config file and store it in cloud storage.
    bucket = util.storage_client().get_bucket(BUCKET)
    pipeline_config = (bucket.blob(config_template_blob_name).download_as_string().decode('utf-8')
        .replace('TO_BE_CONFIGURED/eval_input_path', json.dumps(eval_input_path))
        .replace('TO_BE_CONFIGURED/fine_tune_checkpoint', fine_tune_checkpoint)
        .replace('TO_BE_CONFIGURED/label_map_path', label_map_path)
        .replace('TO_BE_CONFIGURED/num_classes', str(len(sorted_label_list)))
        .replace('TO_BE_CONFIGURED/num_examples', str(eval_frame_count))
        .replace('TO_BE_CONFIGURED/num_training_steps', str(num_training_steps))
        .replace('TO_BE_CONFIGURED/num_visualizations', str(min(100, eval_frame_count)))
        .replace('TO_BE_CONFIGURED/train_batch_size', str(int(batch_size)))
        .replace('TO_BE_CONFIGURED/train_input_path',  json.dumps(train_input_path))
        .replace('TO_BE_CONFIGURED/num_warmup_steps',  str(int(num_warmup_steps)))
        )

    # storage.model_trainer_starting will raise HttpErrorUnprocessableEntity
    # if the max_running_minutes exceeds the team's remaining_training_minutes.
    model_uuid = storage.model_trainer_starting(team_uuid, max_running_minutes)
    model_folder = blob_storage.get_model_folder(team_uuid, model_uuid)

    try:
        pipeline_config_path = blob_storage.store_pipeline_config(model_folder, pipeline_config)
        model_dir = blob_storage.get_model_folder_path(model_folder)

        train_job_id = __get_train_job_id(model_uuid)
        scheduling = {
            'maxRunningTime': '%ds' % int(max_running_minutes * 60),
        }
        train_training_input = {
            'region': 'us-central1',
            'scheduling': scheduling,
            'jobDir': model_dir,
            'args': [
                '--model_dir', model_dir,
                '--pipeline_config_path', pipeline_config_path,
                '--checkpoint_every_n', str(CHECKPOINT_EVERY_N),
            ],
        }
        if use_tpu:
            train_training_input['scaleTier'] = 'BASIC_TPU'
            train_training_input['packageUris'] = [PACKAGE_URI]
            train_training_input['pythonModule'] = 'object_detection.model_main_tf2'
            train_training_input['runtimeVersion'] = '2.6'
            train_training_input['pythonVersion'] = '3.7'
            train_training_input['args'].append('--use_tpu')
            train_training_input['args'].append('true')
        else:
            train_training_input['scaleTier'] = 'BASIC_GPU'
            train_training_input['masterConfig'] = {
                'imageUri': IMAGE_URI,
            }
        train_job = {
            'jobId': train_job_id,
            'trainingInput': train_training_input
        }

        eval_job_id = __get_eval_job_id(model_uuid)
        eval_training_input = {
            'region': 'us-central1',
            'scaleTier': 'BASIC_GPU',
            'masterConfig': {
                'imageUri': IMAGE_URI,
            },
            'jobDir': model_dir,
            'args': [
                '--model_dir', model_dir,
                '--pipeline_config_path', pipeline_config_path,
                '--checkpoint_dir', model_dir,
            ],
        }
        eval_job = {
            'jobId': eval_job_id,
            'trainingInput': eval_training_input,
        }

        ml = get_ml_service()
        parent = __get_parent()

        # Start the eval job first to because it runs slower than the train job. This helps it to
        # not miss the first checkpoint, but does not completely prevent that.
        try:
            if eval_frame_count > 0:
                eval_job_response = ml.projects().jobs().create(parent=parent, body=eval_job).execute()
            else:
                eval_job_response = None
        except:
            logging.critical('model_trainer.start_training_model - creating eval job - except %s' %
                traceback.format_exc().replace('\n', ' ... '))
            raise

        try:
            train_job_response = ml.projects().jobs().create(parent=parent, body=train_job).execute()
        except:
            logging.critical('model_trainer.start_training_model - creating training job - except %s' %
                traceback.format_exc().replace('\n', ' ... '))
            if eval_job_response is not None:
                # Cancel the eval job.
                ml.projects().jobs().cancel(name=get_eval_job_name(model_uuid)).execute()
            raise

    except:
        # storage.failed_to_start_training will adjust the team's remaining training time and delete
        # any model blobs (such as the pipeline config file) that were created.
        storage.model_trainer_failed_to_start(team_uuid, model_folder, max_running_minutes)
        raise

    tensorflow_version = '2'
    model_entity = storage.model_trainer_started(team_uuid, model_uuid, description, model_folder,
        tensorflow_version, use_tpu, dataset_uuids, create_time_ms, max_running_minutes,
        num_training_steps, batch_size, num_warmup_steps, CHECKPOINT_EVERY_N,
        previous_training_steps, starting_model, user_visible_starting_model,
        original_starting_model, fine_tune_checkpoint,
        sorted_label_list, label_map_path, train_input_path, eval_input_path,
        train_frame_count, eval_frame_count, train_negative_frame_count, eval_negative_frame_count,
        train_dict_label_to_count, eval_dict_label_to_count, train_job_response, eval_job_response)
    __start_monitor_training(team_uuid, model_entity['model_uuid'])
    return model_entity


def stop_training_model(team_uuid, model_uuid):
    # storage.retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)
    ml = get_ml_service()
    if is_alive(model_entity['train_job_state']):
        try:
            train_job_name = get_train_job_name(model_uuid)
            ml.projects().jobs().cancel(name=train_job_name).execute()
        except:
            logging.critical('model_trainer.stop_training_model - canceling training job - except %s' %
                traceback.format_exc().replace('\n', ' ... '))
    if model_entity['eval_job']:
        if is_alive(model_entity['eval_job_state']):
            try:
                eval_job_name = get_eval_job_name(model_uuid)
                ml.projects().jobs().cancel(name=eval_job_name).execute()
            except:
                logging.critical('model_trainer.stop_training_model - canceling eval job - except %s' %
                    traceback.format_exc().replace('\n', ' ... '))
    return storage.stop_training_requested(team_uuid, model_uuid)


def get_ml_service():
    payload = cloud_secrets.get("key_json")
    credentials_dict = json.loads(payload)
    scopes = ['https://www.googleapis.com/auth/cloud-platform']
    credentials = service_account.Credentials.from_service_account_info(credentials_dict, scopes=scopes)
    return googleapiclient.discovery.build(
        serviceName='ml', version='v1', credentials=credentials, cache_discovery=False)


def __get_parent():
    # TODO(lizlooney): Is the project id here supposed to be our Google Cloud Project ID?
    return 'projects/%s' % constants.PROJECT_ID


def __get_train_job_id(model_uuid):
    return 'train_%s' % model_uuid


def __get_eval_job_id(model_uuid):
    return 'eval_%s' % model_uuid


def get_train_job_name(model_uuid):
    return '%s/jobs/%s' % (__get_parent(), __get_train_job_id(model_uuid))


def get_eval_job_name(model_uuid):
    return '%s/jobs/%s' % (__get_parent(), __get_eval_job_id(model_uuid))


def is_alive(state):
    return (state == 'QUEUED' or
            state == 'PREPARING' or
            state == 'RUNNING')


def maybe_restart_monitor_training(team_uuid, model_uuid):
    # storage.retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)

    if ('monitor_training_finished' not in model_entity or
            'monitor_training_triggered_time_ms' not in model_entity or
            'monitor_training_active_time_ms' not in model_entity):
        # Some models were trained before these fields were added.
        return False, model_entity

    if model_entity['monitor_training_finished']:
        # The monitor training action finished.
        return False, model_entity

    if model_entity['monitor_training_triggered_time_ms'] == 0:
        # The monitor training action was not triggered yet. It shouldn't be restarted since it
        # hasn't even started the first time yet.
        return False, model_entity

    if model_entity['monitor_training_active_time_ms'] <= model_entity['monitor_training_triggered_time_ms']:
        # The monitor training action was triggered, but not active after it was triggered.
        # Check if it has been <= 3 minutes since it was triggered.
        # Since monitor_training_triggered_time_ms is non-zero, we can use the
        # monitor_training_triggered_time field.
        if datetime.now(timezone.utc) - model_entity['monitor_training_triggered_time'] <= timedelta(minutes=3):
            return False, model_entity
    else:
        # The monitor training action was active after it was triggered.
        # Check if it has been <= 3 minutes since it was active.
        # Since monitor_training_triggered_time_ms and monitor_training_active_time_ms are both
        # non-zero, we can use the monitor_training_triggered_time and monitor_training_active_time
        # fields.
        if datetime.now(timezone.utc) - model_entity['monitor_training_active_time'] <= timedelta(minutes=3):
            return False, model_entity

    return True, __start_monitor_training(team_uuid, model_uuid)


def __start_monitor_training(team_uuid, model_uuid):
    model_entity = storage.prepare_to_start_monitor_training(team_uuid, model_uuid)
    action_parameters = action.create_action_parameters(
        team_uuid, action.ACTION_NAME_MONITOR_TRAINING)
    action_parameters['team_uuid'] = team_uuid
    action_parameters['model_uuid'] = model_uuid
    action.trigger_action_via_blob(action_parameters)
    return model_entity


def make_key(step, tag):
    return storage.make_summary_item_key(step, tag)


def retrieve_tags_and_steps(team_uuid, model_uuid, job_type, value_type):
    # storage.retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)
    list_of_summary_items = storage.get_model_summary_items_all_steps(model_entity, job_type, value_type)
    step_and_tag_pairs = []
    for summary_items in list_of_summary_items:
        for key, item in summary_items.items():
            pair = {
                'step': item['step'],
                'tag': item['tag'],
            }
            step_and_tag_pairs.append(pair)
    return step_and_tag_pairs


def retrieve_summary_items(team_uuid, model_uuid, job_type, value_type, dict_step_to_tags):
    # storage.retrieve_model_entity will raise HttpErrorNotFound
    # if the team_uuid/model_uuid is not found.
    model_entity = storage.retrieve_model_entity(team_uuid, model_uuid)
    model_folder = model_entity['model_folder']
    summary_items_list = []
    for step_string, tags in dict_step_to_tags.items():
        step = int(step_string)
        summary_items = storage.get_model_summary_items(model_entity, job_type, value_type, step)
        for tag in tags:
            key = make_key(step, tag)
            if key not in summary_items:
                continue
            item = summary_items[make_key(step, tag)]
            summary_item = {
                'step': item['step'],
                'tag': item['tag'],
            }
            if value_type == 'scalar':
                summary_item['value'] = item['value']
            elif value_type == 'image':
                exists, image_url = blob_storage.get_event_summary_image_download_url(
                    model_folder, item['job_type'], item['step'], item['tag'], None)
                if not exists:
                    continue
                summary_item['value'] = {
                    'width': item['width'],
                    'height': item['height'],
                    'image_url': image_url,
                }
            summary_items_list.append(summary_item)
    return summary_items_list

