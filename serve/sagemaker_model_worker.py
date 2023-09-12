"""
A model worker that executes the model.
"""
import argparse
import json
from urllib.parse import urlparse
from jsonpath_ng import jsonpath, parse
import re
from typing import List, Dict
import urllib3
import io
import boto3
from botocore.response import StreamingBody
from botocore.eventstream import EventStream
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn
from fastchat.constants import ErrorCode, SERVER_ERROR_MSG
from fastchat.model.model_adapter import add_model_args
from fastchat.serve.model_worker import (
    worker_id,
    logger,
    app,
    BaseModelWorker,
    release_worker_semaphore,
    acquire_worker_semaphore,
    create_background_tasks
)
import fastchat


class StreamIterator:
    def __init__(self, stream):
        self.byte_iterator = iter(stream)
        self.buffer = io.BytesIO()
        self.read_pos = 0

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            self.buffer.seek(self.read_pos)
            line = self.buffer.readline()
            if line and line[-1] == 10:
                self.read_pos += len(line)
                return line[:-1]
            try:
                chunk = next(self.byte_iterator)
            except StopIteration:
                if self.read_pos < self.buffer.getbuffer().nbytes:
                    continue
                raise
            if 'PayloadPart' not in chunk:
                print("Unknown event type:" + chunk)
                continue
            self.buffer.seek(0, io.SEEK_END)
            self.buffer.write(chunk['PayloadPart']['Bytes'])


class SageMakerModelWorker(BaseModelWorker):
    def __init__(
        self,
        ddb_table_name: str,
        worker_id: str,
        model_path: str,
        limit_worker_concurrency: int,
        no_register: bool,
        conv_template: str = None
    ):
        self.ddb_table_name = ddb_table_name
        self.ddb_client = boto3.client("dynamodb")
        self.model_names = self.get_model_list()
        super().__init__(
            "",
            "",
            worker_id,
            model_path,
            self.model_names,
            limit_worker_concurrency,
            conv_template
        )

        logger.info("Starting SageMaker model worker...")
        sm_runtime = boto3.client("sagemaker-runtime")
        self.invoke = sm_runtime.invoke_endpoint_with_response_stream

        logger.info(f"Loading the model {self.model_names} on worker {worker_id} ...")
        self.http = urllib3.PoolManager()
        self.serving_port = 8080
        
    def get_model_list(self):
        return self.ddb_client.get_item(
            TableName=self.ddb_table_name, 
            Key={
                "model_name": {
                    "S": "all-models"
                }
            }
        )["Item"]["model_names"]["S"].split(",")

    def invoke_sagemaker_endpoint(self, params):
        if params["top_p"] == 0:
            params["top_p"] = 0.01
        elif params["top_p"] == 1.0:
            params["top_p"] = 0.99
        if "stop" in params and params["stop"]:
            params["stop"] = [params["stop"]] if isinstance(params["stop"], str) else params["stop"]

        body = self.form_request(
            params,
            self.request["defaults"],
            self.request["mapping"]
        )

        response = self.invoke(
            EndpointName=self.sagemaker_endpoint_name,
            Body=json.dumps(body).encode("utf-8"),
            ContentType="application/json"
        )["Body"]
        if isinstance(response, urllib3.response.HTTPResponse):
            if response.getheader("content-type") == "text/event-stream":
                output = response.stream()
        elif isinstance(response, StreamingBody) or isinstance(response, EventStream):
            output = response
        else:
            print(type(response))
            output = response.read()
        return output

    def form_request(self, params, defaults, mapping):
        for attrib, jpath in mapping.items():
            if attrib in params.keys():
                jsonpath_expr = parse(jpath)
                jsonpath_expr.update(defaults, params[attrib])
        return defaults

    def parse_response(self, response_body, mapping, regex_sub=None):
        res = None
        if regex_sub:
            res = json.loads(re.sub(regex_sub, "", response_body))
        ret = {}
        for attrib, jpath in mapping.items():
            jsonpath_expr = parse(jpath)
            results = jsonpath_expr.find(res)
            if results and len(results) > 0:
                ret[attrib] = results[0].value
        return ret
    
    def get_model_config(self, model_name):
        resp = self.ddb_client.get_item(
            TableName=self.ddb_table_name, 
            Key={
                "model_name": {
                "S": model_name
                }
            }
        )["Item"]
        json_config = json.loads(
            resp["endpoint_req_config"]["S"],
            strict=False
            )
        if "response" not in json_config.keys() or "request" not in json_config.keys():
            raise ValueError("Worker config file must container 'request' and 'response' keys.")
        return (
            resp["endpoint_name"]["S"],
            json_config["request"],
            json_config["response"]
        )

    def generate_stream_gate(self, params):
        self.call_ct += 1
        model_name = params["model"]
        (
            self.sagemaker_endpoint_name,
            self.request,
            self.response
        ) = self.get_model_config(model_name)

        try:
            text = ""
            for line in StreamIterator(self.invoke_sagemaker_endpoint(params)):
                if line:
                    output = self.parse_response(
                        line.decode("utf-8"),
                        self.response["mapping"],
                        self.response["regex_sub"]
                    )
                    if "error" in output:
                        raise ValueError(output)
                    if "stop" in params:
                        stop_token = params["stop"] if isinstance(params["stop"], list) else list(params["stop"])
                    else:
                        stop_token = None
                    if output["text"] not in stop_token:
                        text += output["text"]
                    else:
                        break
                    output["text"] = text
                output["error_code"] = 0
                yield json.dumps(output).encode() + b"\0"
        except (ValueError, RuntimeError) as e:
            ret = {
                "text": f"{SERVER_ERROR_MSG}\n\n({e})",
                "error_code": ErrorCode.INTERNAL_ERROR,
            }
            yield json.dumps(ret).encode() + b"\0"

    def generate_gate(self, params):
        for x in self.generate_stream_gate(params):
            pass
        return json.loads(x[:-1].decode())


@app.post("/worker_generate_stream")
async def api_generate_stream(request: Request):
    params = await request.json()
    await acquire_worker_semaphore()
    generator = worker.generate_stream_gate(params)
    background_tasks = create_background_tasks()
    return StreamingResponse(generator, background=background_tasks)


@app.post("/worker_generate")
async def api_generate(request: Request):
    params = await request.json()
    await acquire_worker_semaphore()
    output = worker.generate_gate(params)
    release_worker_semaphore()
    return JSONResponse(output)


@app.post("/worker_get_status")
async def api_get_status(request: Request):
    return worker.get_status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=21002)
    add_model_args(parser)
    parser.add_argument(
        "--conv-template", type=str, default=None, help="Conversation prompt template."
    )
    parser.add_argument(
        "--limit-worker-concurrency",
        type=int,
        default=5,
        help="Limit the model concurrency to prevent OOM.",
    )
    parser.add_argument("--stream-interval", type=int, default=2)
    parser.add_argument("--no-register", action="store_true")
    parser.add_argument("--ddb-table-name", type=str, default="")
    args = parser.parse_args()
    logger.info(f"args: {args}")

    worker = SageMakerModelWorker(
        args.ddb_table_name,
        worker_id,
        args.model_path,
        args.limit_worker_concurrency,
        no_register=args.no_register,
        conv_template=args.conv_template
    )

    fastchat.serve.model_worker.worker = worker
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")