import os
import json
import uuid
from datetime import datetime, timezone

import boto3

dynamodb = boto3.resource("dynamodb")

receipts_table = dynamodb.Table(
    os.environ["RECEIPTS_TABLE"]
)

sessions_table = dynamodb.Table(
    os.environ["SESSIONS_TABLE"]
)

def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }

def get_token_from_event(event):
    headers = event.get("headers") or {}

    authorization = headers.get("authorization") or headers.get("Authorization")

    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        return None

    return authorization.replace("Bearer ", "", 1).strip()

def get_session(token):
    result = sessions_table.get_item(
        Key={"token": token}
    )

    return result.get("Item")

def handle_upload(event):
    token = get_token_from_event(event)

    if not token:
        return response(
            401,
            {"message": "Missing token"}
        )

    session = get_session(token)

    if not session:
        return response(
            401,
            {"message": "Invalid session"}
        )

    receipt_id = str(uuid.uuid4())

    receipts_table.put_item(
        Item={
            "userEmail": session["email"],
            "receiptId": receipt_id,
            "createdAt": datetime.now(
                timezone.utc
            ).isoformat(),
            "status": "CREATED",
        }
    )

    return response(
        201,
        {
            "receiptId": receipt_id,
            "message": "Receipt created",
        },
    )
    
def lambda_handler(event, context):
    route = event.get("rawPath")
    method = event.get("requestContext", {}).get("http", {}).get("method")

    if route == "/receipts/upload" and method == "POST":
        return handle_upload(event)

    return response(404, {"message": "Route not found."})
