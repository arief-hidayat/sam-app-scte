import json
import boto3
import os
import time
from botocore.exceptions import ClientError
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools import Logger
from aws_lambda_powertools import Tracer
from aws_lambda_powertools import Metrics

app = APIGatewayRestResolver()
tracer = Tracer()
logger = Logger(level="INFO")
metrics = Metrics(namespace="Powertools")

dynamodb = boto3.resource('dynamodb')
medialive = boto3.client('medialive', region_name='ap-southeast-1')
table = dynamodb.Table(os.environ['CHANNEL_SPLICE_TABLE'])


@app.put("/channels/<channel_id>")
@tracer.capture_method
def update_scte_marker(channel_id):
    try:
        logger.info(
            f"Finding record of channel {channel_id} not found in DB {os.environ['CHANNEL_SPLICE_TABLE']}")

        # Try to get the item from DynamoDB
        response = table.get_item(Key={'channelId': channel_id})

        if 'Item' not in response:
            # If not found, generate new spliceEventId and store in DynamoDB
            splice_event_id = f"splice_{int(time.time())}"
            logger.info(f"Channel {channel_id} not found in DB yet. Create SCTE35 splice insert with spliceEventId {splice_event_id}")
            table.put_item(
                Item={
                    'channelId': channel_id,
                    'spliceEventId': splice_event_id
                }
            )

            # Call MediaLive API for splice_insert
            medialive_response = medialive.batch_update_schedule(
                ChannelId=channel_id,
                Creates={
                    'ScheduleActions': [
                        {
                            'ActionName': splice_event_id,
                            'ScheduleActionStartSettings': {
                                'ImmediateModeScheduleActionStartSettings': {}
                            },
                            'ScheduleActionSettings': {
                                'Scte35SpliceInsertSettings': {
                                    'SpliceEventId': int(splice_event_id.split('_')[1]),
                                    # 'Duration': 0
                                }
                            }
                        }
                    ]
                }
            )

            return {
                'statusCode': 200,
                'body': json.dumps('Splice insert scheduled')
            }
        else:
            # If found, use stored spliceEventId for return_to_network
            splice_event_id = response['Item']['spliceEventId']
            logger.info(f"Channel {channel_id} found in DB. Returning to network with spliceEventId {splice_event_id}")
            medialive_response = medialive.batch_update_schedule(
                ChannelId=channel_id,
                Creates={
                    'ScheduleActions': [
                        {
                            'ActionName': f"return_{splice_event_id}",
                            'ScheduleActionStartSettings': {
                                'ImmediateModeScheduleActionStartSettings': {}
                            },
                            'ScheduleActionSettings': {
                                'Scte35ReturnToNetworkSettings': {
                                    'SpliceEventId': int(splice_event_id.split('_')[1])
                                }
                            }
                        }
                    ]
                }
            )
            # delete the record from DynamoDB
            table.delete_item(Key={'channelId': channel_id})

            return {
                'statusCode': 200,
                'body': json.dumps('Return to network scheduled')
            }

    except ClientError as e:
        logger.error(e.response['Error']['Message'])
        return {
            'statusCode': 500,
            'body': json.dumps(f"Error: {str(e)}")
        }

# Enrich logging with contextual information from Lambda
@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
# Adding tracer
# See: https://awslabs.github.io/aws-lambda-powertools-python/latest/core/tracer/
@tracer.capture_lambda_handler
# ensures metrics are flushed upon request completion/failure and capturing ColdStart metric
@metrics.log_metrics(capture_cold_start_metric=True)
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)