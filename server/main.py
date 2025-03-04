# Copyright 2020 Google LLC
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
import logging

# My Modules
from app_engine import action
import cf_action


logging.getLogger().setLevel(logging.INFO)


# cloud functions

def perform_action(data, context):
    start_time = datetime.now(timezone.utc)
    if data['bucket'] == action.BUCKET_ACTION_PARAMETERS:
        time_limit = start_time + timedelta(seconds=500)
        cf_action.perform_action_from_blob(data['name'], time_limit)
    else:
        logging.critical('perform_action called on invalid bucket ' + action.BUCKET_ACTION_PARAMETERS)
    return 'OK'
