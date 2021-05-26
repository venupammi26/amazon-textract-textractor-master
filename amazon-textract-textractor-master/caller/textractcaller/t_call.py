from typing import Union, List, Optional
from enum import Enum
import os
from dataclasses import dataclass
import boto3
import time
import logging
import json
import io

Textract_Features = Enum('Textract_Features', ["FORMS", "TABLES"], start=0)
Textract_Types = Enum(
    'Textract_Types',
    ["WORD", "LINE", "TABLE", "CELL", "KEY", "VALUE", "FORM"])
Textract_API = Enum('Textract_API', ["ANALYZE", "DETECT"], start=0)

logger = logging.getLogger(__name__)


@dataclass
class NotificationChannel():
    def __init__(self, role_arn: str, sns_topic_arn: str):
        if not role_arn and not sns_topic_arn:
            raise ValueError(
                "both role_arn and sns_topic_arn have to be specified")
        self.role_arn = role_arn
        self.sns_topic_arn = sns_topic_arn

    def get_dict(self):
        return {'SNSTopicArn': self.sns_topic_arn, 'RoleArn': self.role_arn}


@dataclass
class OutputConfig():
    def __init__(self, s3_bucket: str, s3_prefix: str):
        if not s3_bucket and not s3_prefix:
            raise ValueError(
                "both s3_bucket and s3_prefix have to be specified")
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix

    def get_dict(self):
        return {'S3Bucket': self.s3_bucket, 'S3Prefix': self.s3_prefix}


@dataclass
class DocumentLocation():
    def __init__(self, s3_bucket: str, s3_prefix: str, version: str = None):
        if not s3_bucket and not s3_prefix:
            raise ValueError(
                "both s3_bucket and s3_prefix have to be specified")
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.s3_version = version

    def get_dict(self):
        return_value = {
            'S3Object': {
                'Bucket': self.s3_bucket,
                'Name': self.s3_prefix
            }
        }
        if self.s3_version:
            return_value['S3Object']['Version'] = self.s3_version
        return return_value


@dataclass
class Document():
    def __init__(self,
                 byte_data: bytes = None,
                 s3_bucket: str = None,
                 s3_prefix: str = None,
                 version: str = None):
        if byte_data and s3_bucket:
            raise ValueError("only one allowed, byte_data or s3_bucket")
        if not byte_data and not (s3_bucket and s3_prefix):
            raise ValueError(
                "when not passing in byte_data, have to specify both s3_bucket and s3_prefix"
            )
        self.byte_data = byte_data
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.s3_version = version

    def get_dict(self):
        if self.byte_data:
            return {'Bytes': self.byte_data}
        else:
            return_value = {
                'S3Object': {
                    'Bucket': self.s3_bucket,
                    'Name': self.s3_prefix
                }
            }
            if self.s3_version:
                return_value['S3Object']['Version'] = self.s3_version
            return return_value


pdf_suffixes = ['.pdf']
image_suffixes = ['.png', '.jpg', '.jpeg']
supported_suffixes = pdf_suffixes + image_suffixes


def generate_request_params(
        document_location: DocumentLocation = None,
        document: Document = None,
        features: Optional[List[Textract_Features]] = None,
        client_request_token: str = None,
        job_tag: str = None,
        notification_channel: Optional[NotificationChannel] = None,
        output_config: Optional[OutputConfig] = None,
        kms_key_id: str = None) -> dict:
    params = {}
    if document_location and document:
        raise ValueError("Only one at a time, documentat_location or document")
    if document_location:
        params['DocumentLocation'] = document_location.get_dict()
    if document:
        params['Document'] = document.get_dict()
    if features:
        params['FeatureTypes'] = [x.name for x in features]
    if client_request_token:
        params['ClientRequestToken'] = client_request_token
    if job_tag:
        params['JobTag'] = job_tag
    if notification_channel:
        params['NotificationChannel'] = notification_channel.get_dict()
    if output_config:
        params['OutputConfig'] = output_config.get_dict()
    if kms_key_id:
        params['KMSKeyId'] = kms_key_id
    return params


def get_job_response(job_id: str = None,
                     textract_api: Textract_API = Textract_API.DETECT,
                     extra_args=None,
                     boto3_textract_client= None):
    if not boto3_textract_client:
        raise ValueError("Need boto3_textract_client")
    if extra_args == None:
        extra_args = {}
    if textract_api == Textract_API.DETECT:
        return boto3_textract_client.get_document_text_detection(JobId=job_id,
                                                                 **extra_args)
    else:
        return boto3_textract_client.get_document_analysis(JobId=job_id,
                                                           **extra_args)


# TODO would like to use a package for that functionality
def get_s3_output_config_keys(output_config: OutputConfig, job_id: str,
                              s3_client):
    if not output_config or not job_id:
        raise ValueError("no output_config or job_id")
    if not s3_client:
        s3_client = boto3.client("s3")
    params = {
        'Bucket': output_config.s3_bucket.strip('/'),
        'Prefix': output_config.s3_prefix.strip('/') + "/" + job_id
    }
    logger.info(f"s3-params {params}")

    while True:
        response = s3_client.list_objects_v2(**params)

        for object in [
                o for o in response.get('Contents', [])
                if o['Key'].split('/')[-1].isnumeric()
        ]:
            yield object['Key']

        params['ContinuationToken'] = response.get('NextContinuationToken')
        if not params['ContinuationToken']:
            break


def get_full_json_from_output_config(output_config: OutputConfig = None,
                                     job_id: str = None,
                                     s3_client = None)->dict:
    if not output_config or not job_id:
        raise ValueError("no output_config or job_id")
    if not output_config.s3_bucket or not output_config.s3_prefix:
        raise ValueError("no output_config or job_id")
    if not s3_client:
        s3_client = boto3.client("s3")

    result_value = {}
    for key in get_s3_output_config_keys(output_config=output_config,
                                          job_id=job_id,
                                          s3_client=s3_client):
        logger.info(f"found keys: {key}")
        response = json.loads(s3_client.get_object(Bucket=output_config.s3_bucket,
                                        Key=key)['Body'].read().decode('utf-8'))
        if 'Blocks' in result_value:
            result_value['Blocks'].extend(response['Blocks'])
        else:
            result_value = response
    if 'NextToken' in result_value:
        del result_value['NextToken']
    return result_value


def get_full_json(job_id: str = None,
                  textract_api: Textract_API = Textract_API.DETECT,
                  boto3_textract_client=None)->dict:
    """returns full json for call, even when response is chunked"""
    logger.debug(
        f"get_full_json: job_id: {job_id}, Textract_API: {textract_api.name}")
    job_response = get_job_response(
        job_id=job_id,
        textract_api=textract_api,
        boto3_textract_client=boto3_textract_client)
    logger.debug(f"job_response for job_id: {job_id}")
    job_status = job_response['JobStatus']
    while job_status == "IN_PROGRESS":
        job_response = get_job_response(
            job_id=job_id,
            textract_api=textract_api,
            boto3_textract_client=boto3_textract_client)
        job_status = job_response['JobStatus']
        time.sleep(1)
    if job_status == 'SUCCEEDED':
        result_value = {}
        extra_args = {}
        while True:
            textract_results = get_job_response(
                job_id=job_id,
                textract_api=textract_api,
                extra_args=extra_args,
                boto3_textract_client=boto3_textract_client)
            if 'Blocks' in result_value:
                result_value['Blocks'].extend(textract_results['Blocks'])
            else:
                result_value = textract_results
            if 'NextToken' in textract_results:
                logger.debug(f"got next token {textract_results['NextToken']}")
                extra_args['NextToken'] = textract_results['NextToken']
            else:
                break
        if 'NextToken' in result_value:
            del result_value['NextToken']
        return result_value
    else:
        logger.error(f"{job_response}")
        raise Exception(
            f"job_status not SUCCEEDED. job_status: {job_status}, message: {job_response['StatusMessage']}"
        )


def call_textract(input_document: Union[str, bytes],
                  features: List[Textract_Features] = None,
                  output_config: OutputConfig = None,
                  kms_key_id: str = None,
                  job_tag: str = None,
                  notification_channel: NotificationChannel = None,
                  client_request_token: str = None,
                  return_job_id: bool = False,
                  force_async_api: bool = False,
                  boto3_textract_client=None) -> dict:
    """
    calls Textract and returns a response (either full json as string (json.dumps)or the job_id when return_job_id=True)
    input_document: points to document on S3 when string starts with s3://
                    points to local file when string does not start with s3://
                    or bytearray when object is in memory
    s3_output_url: s3 output location in the form of s3://<bucket>/<key>
    return_job_id: return job_id instead of full json in case calling functions handles async process flow
    force_async_api: when passing in an image default is to call sync API, this forces the async API to be called (input-document has to be on S3)
    client_request_token: passed down to Textract API
    job_tag: passed down to Textract API
    boto_3_textract_client: pass in boto3 client (to overcome missing region in environmnent, e. g.)
    returns: dict with either Textract response or async API response (incl. the JobId)
    raises LimitExceededException when receiving LimitExceededException from Textract API. Expectation is to handle in calling function
    """
    logger.debug("call_textract")
    if not boto3_textract_client:
        textract = boto3.client("textract")
    else:
        textract = boto3_textract_client
    is_s3_document: bool = False
    s3_bucket = ""
    s3_key = ""
    result_value = {}
    if isinstance(input_document, str):
        if len(input_document) > 7 and input_document.lower().startswith(
                "s3://"):
            is_s3_document = True
            input_document = input_document.replace("s3://", "")
            s3_bucket, s3_key = input_document.split("/", 1)
        ext: str = ""
        _, ext = os.path.splitext(input_document)
        ext = ext.lower()

        is_pdf: bool = (ext != None and ext.lower() in pdf_suffixes)
        if is_pdf and not is_s3_document:
            raise ValueError("PDF only supported when located on S3")
        if not is_s3_document and force_async_api:
            raise ValueError("when forcing async, document has to be on s3")
        if not is_s3_document and output_config:
            raise ValueError(
                "only can have s3_output_url for async processes with document location on s3"
            )
        if notification_channel and not return_job_id:
            raise ValueError(
                "when submitting notification_channel, has to also expect the job_id as result atm."
            )
        # ASYNC
        if is_pdf or force_async_api and is_s3_document:
            logger.debug(f"is_pdf or force_async_api and is_s3_document")
            params = generate_request_params(
                document_location=DocumentLocation(s3_bucket=s3_bucket,
                                                   s3_prefix=s3_key),
                features=features,
                output_config=output_config,
                notification_channel=notification_channel,
                kms_key_id=kms_key_id,
                client_request_token=client_request_token,
                job_tag=job_tag,
            )

            if features:
                textract_api = Textract_API.ANALYZE
                submission_status = textract.start_document_analysis(**params)
            else:
                textract_api = Textract_API.DETECT
                submission_status = textract.start_document_text_detection(
                    **params)
            if submission_status["ResponseMetadata"]["HTTPStatusCode"] == 200:
                if return_job_id:
                    return submission_status
                else:
                    result_value = get_full_json(
                        submission_status['JobId'],
                        textract_api=textract_api,
                        boto3_textract_client=textract)
            else:
                raise Exception(
                    f"Got non-200 response code: {submission_status}")

        elif ext in image_suffixes:
            # s3 file
            if is_s3_document:
                params = generate_request_params(
                    document=Document(s3_bucket=s3_bucket, s3_prefix=s3_key),
                    features=features,
                    output_config=output_config,
                    kms_key_id=kms_key_id,
                    notification_channel=notification_channel)
                if features:
                    result_value = textract.analyze_document(**params)
                else:
                    result_value = textract.detect_document_text(**params)
            # local file
            else:
                with open(input_document, 'rb') as input_file:
                    doc_bytes: bytearray = bytearray(input_file.read())
                    params = generate_request_params(
                        document=Document(byte_data=doc_bytes),
                        features=features,
                    )

                    if features:
                        result_value = textract.analyze_document(**params)
                    else:
                        result_value = textract.detect_document_text(**params)

    # got bytearray, calling sync API
    elif isinstance(input_document, (bytes, bytearray)):
        logger.debug("processing bytes or bytearray")
        if force_async_api:
            raise Exception("cannot run async for bytearray")
        params = generate_request_params(
            document=Document(byte_data=input_document),
            features=features,
        )
        if features:
            result_value = textract.analyze_document(**params)
        else:
            result_value = textract.detect_document_text(**params)
    else:
        raise ValueError(f"unsupported input_document type: {type(input_document)}")

    return result_value
