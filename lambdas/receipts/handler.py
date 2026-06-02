import os
import json
import uuid
import base64
from datetime import datetime, timezone

import boto3


dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")

receipts_table = dynamodb.Table(os.environ["RECEIPTS_TABLE"])
sessions_table = dynamodb.Table(os.environ["SESSIONS_TABLE"])
receipt_images_bucket = os.environ["RECEIPT_IMAGES_BUCKET"]


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def parse_body(event):
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return None


def get_token_from_event(event):
    headers = event.get("headers") or {}
    authorization = headers.get("authorization") or headers.get("Authorization")

    if not authorization or not authorization.startswith("Bearer "):
        return None

    return authorization.replace("Bearer ", "", 1).strip()


def get_session(token):
    result = sessions_table.get_item(Key={"token": token})
    return result.get("Item")


def require_session(event):
    token = get_token_from_event(event)

    if not token:
        return None, response(401, {"message": "Missing token"})

    session = get_session(token)

    if not session:
        return None, response(401, {"message": "Invalid session"})

    return session, None


def decode_image_base64(image_base64):
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]

    return base64.b64decode(image_base64)


def get_file_extension(content_type):
    if content_type == "image/png":
        return "png"

    if content_type in ["image/jpeg", "image/jpg"]:
        return "jpg"

    return None


def handle_upload(event):
    session, error_response = require_session(event)

    if error_response:
        return error_response

    body = parse_body(event)

    if body is None:
        return response(400, {"message": "Invalid JSON body."})

    image_base64 = body.get("imageBase64")
    content_type = body.get("contentType", "image/png")

    if not image_base64:
        return response(400, {"message": "imageBase64 is required."})

    file_extension = get_file_extension(content_type)

    if not file_extension:
        return response(400, {"message": "Only PNG and JPG images are supported."})

    try:
        image_bytes = decode_image_base64(image_base64)
    except Exception:
        return response(400, {"message": "Invalid base64 image."})

    receipt_id = str(uuid.uuid4())
    user_email = session["email"]

    image_s3_key = f"receipts/{user_email}/{receipt_id}.{file_extension}"

    s3.put_object(
        Bucket=receipt_images_bucket,
        Key=image_s3_key,
        Body=image_bytes,
        ContentType=content_type,
    )

    now = datetime.now(timezone.utc).isoformat()

    receipts_table.put_item(
        Item={
            "userEmail": user_email,
            "receiptId": receipt_id,
            "createdAt": now,
            "updatedAt": now,

            "status": "IMAGE_UPLOADED",

            "imageS3Key": image_s3_key,

            "storeName": None,
            "receiptDate": None,
            "subtotal": None,
            "tax": None,
            "total": None,

            "receiptJson": None,
        }
    )

    return response(
        201,
        {
            "receiptId": receipt_id,
            "message": "Receipt image uploaded",
            "imageS3Key": image_s3_key,
            "status": "IMAGE_UPLOADED",
        },
    )


def get_openai_api_key():
    secret_name = os.environ["OPENAI_SECRET_NAME"]

    response = secrets_client.get_secret_value(
        SecretId=secret_name
    )

    secret_json = json.loads(
        response["SecretString"]
    )

    return secret_json["API_KEY"]


def handle_process(event):
    session, error_response = require_session(event)

    if error_response:
        return error_response

    path_parameters = event.get("pathParameters") or {}
    receipt_id = path_parameters.get("receiptId")

    if not receipt_id:
        return response(400, {"message": "receiptId is required."})

    user_email = session["email"]

    result = receipts_table.get_item(
        Key={
            "userEmail": user_email,
            "receiptId": receipt_id,
        }
    )

    receipt = result.get("Item")

    if not receipt:
        return response(404, {"message": "Receipt not found."})

    if not receipt.get("imageS3Key"):
        return response(400, {"message": "Receipt does not have an uploaded image."})

    api_key = get_openai_api_key()

    return response(
        200,
        {
            "message": "Ready to process receipt with OpenAI.",
            "receiptId": receipt_id,
            "imageS3Key": receipt["imageS3Key"],
            "hasApiKey": bool(api_key),
        },
    )


def lambda_handler(event, context):
    route = event.get("rawPath")
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if route == "/receipts/upload" and method == "POST":
        return handle_upload(event)

    if route and route.startswith("/receipts/process/") and method == "POST":
        return handle_process(event)

    return response(404, {"message": "Route not found."})
