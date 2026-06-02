import os
import json
import uuid
import base64
import urllib.request
import urllib.error
from decimal import Decimal
from datetime import datetime, timezone
import boto3
from boto3.dynamodb.conditions import Key


dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
secrets_client = boto3.client("secretsmanager")

receipts_table = dynamodb.Table(os.environ["RECEIPTS_TABLE"])
sessions_table = dynamodb.Table(os.environ["SESSIONS_TABLE"])
receipt_images_bucket = os.environ["RECEIPT_IMAGES_BUCKET"]

OPENAI_MODEL = "gpt-4o-mini"


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(jsonify(body)),
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


def session_is_expired(session):
    expires_at = session.get("expiresAt")

    if not expires_at:
        return False

    now_epoch = int(datetime.now(timezone.utc).timestamp())

    return int(expires_at) < now_epoch


def require_session(event):
    token = get_token_from_event(event)

    if not token:
        return None, response(401, {"message": "Missing token"})

    session = get_session(token)

    if not session:
        return None, response(401, {"message": "Invalid session"})

    if session_is_expired(session):
        return None, response(401, {"message": "Session expired"})

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

def jsonify(value):
    if isinstance(value, list):
        return [jsonify(item) for item in value]

    if isinstance(value, dict):
        return {key: jsonify(val) for key, val in value.items()}

    if isinstance(value, Decimal):
        if value % 1 == 0:
            return int(value)
        return float(value)

    return value


def decimalize(value):
    if isinstance(value, list):
        return [decimalize(item) for item in value]

    if isinstance(value, dict):
        return {key: decimalize(val) for key, val in value.items()}

    if isinstance(value, float):
        return Decimal(str(value))

    return value


def get_openai_api_key():
    secret_name = os.environ["OPENAI_SECRET_NAME"]

    secret_response = secrets_client.get_secret_value(SecretId=secret_name)
    secret_json = json.loads(secret_response["SecretString"])

    return secret_json["API_KEY"]


def get_receipt_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schemaVersion",
            "store",
            "location",
            "lineItems",
            "summary",
        ],
        "properties": {
            "schemaVersion": {
                "type": "string",
                "enum": ["1.0"],
            },
            "store": {
                "type": "string",
            },
            "location": {
                "type": "string",
            },
            "lineItems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "id",
                        "description",
                        "category",
                        "quantity",
                        "unit",
                        "unitPrice",
                        "totalPrice",
                    ],
                    "properties": {
                        "id": {"type": "integer"},
                        "description": {"type": "string"},
                        "category": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                        "unitPrice": {"type": "number"},
                        "totalPrice": {"type": "number"},
                    },
                },
            },
            "summary": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "subtotal",
                    "tax",
                    "total",
                    "itemsSold",
                ],
                "properties": {
                    "subtotal": {"type": "number"},
                    "tax": {"type": "number"},
                    "total": {"type": "number"},
                    "itemsSold": {"type": "integer"},
                },
            },
        },
    }


def call_openai_receipt_extraction(api_key, image_data_url):
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You extract structured receipt data from receipt images. "
                            "Return only data visible or reasonably inferable from the receipt. "
                            "Use empty strings for missing text. Use 0 for missing numbers. "
                            "Categorize each line item into a short practical shopping category."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Extract this receipt into the required JSON schema.",
                    },
                    {
                        "type": "input_image",
                        "image_url": image_data_url,
                    },
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "receipt_extraction",
                "strict": True,
                "schema": get_receipt_schema(),
            }
        },
    }

    request = urllib.request.Request(
        url="https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as openai_response:
            response_body = openai_response.read().decode("utf-8")
            return json.loads(response_body)

    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8")
        raise Exception(f"OpenAI API error {error.code}: {error_body}")


def extract_output_json(openai_response):
    for output in openai_response.get("output", []):
        if output.get("type") != "message":
            continue

        for item in output.get("content", []):
            if item.get("type") == "refusal":
                raise Exception(f"OpenAI refused: {item.get('refusal')}")

            if item.get("type") == "output_text":
                return json.loads(item.get("text", "{}"))

    raise Exception("Could not find structured output text in OpenAI response.")


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
            "contentType": content_type,
            "storeName": "",
            "location": "",
            "receiptDate": "",
            "subtotal": Decimal("0"),
            "tax": Decimal("0"),
            "total": Decimal("0"),
            "itemsSold": 0,
            "schemaVersion": "1.0",
            "receiptJson": {},
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

    image_s3_key = receipt.get("imageS3Key")

    if not image_s3_key:
        return response(400, {"message": "Receipt does not have an uploaded image."})

    try:
        s3_response = s3.get_object(
            Bucket=receipt_images_bucket,
            Key=image_s3_key,
        )

        image_bytes = s3_response["Body"].read()
        content_type = receipt.get("contentType", "image/png")
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        image_data_url = f"data:{content_type};base64,{image_base64}"

        api_key = get_openai_api_key()

        openai_response = call_openai_receipt_extraction(
            api_key=api_key,
            image_data_url=image_data_url,
        )

        receipt_json = extract_output_json(openai_response)

        summary = receipt_json.get("summary", {})

        now = datetime.now(timezone.utc).isoformat()

        receipts_table.update_item(
            Key={
                "userEmail": user_email,
                "receiptId": receipt_id,
            },
            UpdateExpression=(
                "SET #status = :status, "
                "updatedAt = :updatedAt, "
                "schemaVersion = :schemaVersion, "
                "receiptJson = :receiptJson, "
                "storeName = :storeName, "
                "#location = :location, "
                "subtotal = :subtotal, "
                "tax = :tax, "
                "#total = :total, "
                "itemsSold = :itemsSold "
                "REMOVE processingError"
            ),
            ExpressionAttributeNames={
                "#status": "status",
                "#location": "location",
                "#total": "total",
            },
            ExpressionAttributeValues={
                ":status": "PROCESSED",
                ":updatedAt": now,
                ":schemaVersion": receipt_json.get("schemaVersion", "1.0"),
                ":receiptJson": decimalize(receipt_json),
                ":storeName": receipt_json.get("store", ""),
                ":location": receipt_json.get("location", ""),
                ":subtotal": Decimal(str(summary.get("subtotal", 0))),
                ":tax": Decimal(str(summary.get("tax", 0))),
                ":total": Decimal(str(summary.get("total", 0))),
                ":itemsSold": int(summary.get("itemsSold", 0)),
            },
        )

        return response(
            200,
            {
                "message": "Receipt processed",
                "receiptId": receipt_id,
                "status": "PROCESSED",
                "receipt": receipt_json,
            },
        )

    except Exception as error:
        print("Processing error:", str(error))

        receipts_table.update_item(
            Key={
                "userEmail": user_email,
                "receiptId": receipt_id,
            },
            UpdateExpression="SET #status = :status, updatedAt = :updatedAt, processingError = :processingError",
            ExpressionAttributeNames={
                "#status": "status",
            },
            ExpressionAttributeValues={
                ":status": "PROCESSING_FAILED",
                ":updatedAt": datetime.now(timezone.utc).isoformat(),
                ":processingError": str(error),
            },
        )

        return response(
            500,
            {
                "message": "Receipt processing failed.",
                "error": str(error),
            },
        )


def handle_list_receipts(event):
    session, error_response = require_session(event)

    if error_response:
        return error_response

    result = receipts_table.query(
        KeyConditionExpression=Key("userEmail").eq(session["email"]),
        ScanIndexForward=False,
    )

    receipts = []

    for item in result.get("Items", []):
        receipts.append(
            {
                "receiptId": item.get("receiptId"),
                "createdAt": item.get("createdAt"),
                "updatedAt": item.get("updatedAt"),
                "status": item.get("status"),
                "storeName": item.get("storeName", ""),
                "location": item.get("location", ""),
                "subtotal": float(item.get("subtotal", 0)),
                "tax": float(item.get("tax", 0)),
                "total": float(item.get("total", 0)),
                "itemsSold": int(item.get("itemsSold", 0)),
                "schemaVersion": item.get("schemaVersion", "1.0"),
            }
        )

    return response(200, {"receipts": receipts})


def handle_get_receipt(event):
    session, error_response = require_session(event)

    if error_response:
        return error_response

    receipt_id = (event.get("pathParameters") or {}).get("receiptId")

    if not receipt_id:
        return response(400, {"message": "receiptId is required."})

    result = receipts_table.get_item(
        Key={
            "userEmail": session["email"],
            "receiptId": receipt_id,
        }
    )

    receipt = result.get("Item")

    if not receipt:
        return response(404, {"message": "Receipt not found."})

    return response(
        200,
        {
            "receiptId": receipt.get("receiptId"),
            "createdAt": receipt.get("createdAt"),
            "updatedAt": receipt.get("updatedAt"),
            "status": receipt.get("status"),
            "imageS3Key": receipt.get("imageS3Key"),
            "storeName": receipt.get("storeName", ""),
            "location": receipt.get("location", ""),
            "subtotal": float(receipt.get("subtotal", 0)),
            "tax": float(receipt.get("tax", 0)),
            "total": float(receipt.get("total", 0)),
            "itemsSold": int(receipt.get("itemsSold", 0)),
            "schemaVersion": receipt.get("schemaVersion", "1.0"),
            "receiptJson": receipt.get("receiptJson", {}),
            "processingError": receipt.get("processingError"),
        },
    )


def handle_delete_receipt(event):
    session, error_response = require_session(event)

    if error_response:
        return error_response

    receipt_id = (event.get("pathParameters") or {}).get("receiptId")

    if not receipt_id:
        return response(400, {"message": "receiptId is required."})

    result = receipts_table.get_item(
        Key={
            "userEmail": session["email"],
            "receiptId": receipt_id,
        }
    )

    receipt = result.get("Item")

    if not receipt:
        return response(404, {"message": "Receipt not found."})

    image_s3_key = receipt.get("imageS3Key")

    if image_s3_key:
        s3.delete_object(
            Bucket=receipt_images_bucket,
            Key=image_s3_key,
        )

    receipts_table.delete_item(
        Key={
            "userEmail": session["email"],
            "receiptId": receipt_id,
        }
    )

    return response(
        200,
        {
            "message": "Receipt deleted",
            "receiptId": receipt_id,
        },
    )
    

def lambda_handler(event, context):
    route = event.get("rawPath")
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if route == "/receipts/upload" and method == "POST":
        return handle_upload(event)

    if route and route.startswith("/receipts/process/") and method == "POST":
        return handle_process(event)

    if route == "/receipts" and method == "GET":
        return handle_list_receipts(event)

    if route and route.startswith("/receipts/") and method == "GET":
        return handle_get_receipt(event)

    if route and route.startswith("/receipts/") and method == "DELETE":
        return handle_delete_receipt(event)

    return response(404, {"message": "Route not found."})

