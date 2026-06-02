from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
)
from constructs import Construct
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager

from aws_cdk.aws_apigatewayv2_alpha import (
    HttpApi,
    CorsHttpMethod,
    HttpMethod,
)
from aws_cdk.aws_apigatewayv2_integrations_alpha import HttpLambdaIntegration


class ReceiptsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        
        openai_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "OpenAiSecret",
            "prod/openai",
        )

        receipts_table = dynamodb.Table(
            self,
            "ReceiptsTable",
            table_name="ReceiptReceipts",
            partition_key=dynamodb.Attribute(
                name="userEmail",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="receiptId",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        sessions_table = dynamodb.Table.from_table_name(
            self,
            "ImportedSessionsTable",
            "ReceiptSessions",
        )

        receipt_images_bucket = s3.Bucket(
            self,
            "ReceiptImagesBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        receipts_lambda = _lambda.Function(
            self,
            "ReceiptsLambda",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=_lambda.Code.from_asset("lambdas/receipts"),
            timeout=Duration.seconds(30),
            environment={
                "RECEIPTS_TABLE": receipts_table.table_name,
                "SESSIONS_TABLE": "ReceiptSessions",
                "RECEIPT_IMAGES_BUCKET": receipt_images_bucket.bucket_name,
                "OPENAI_SECRET_NAME": "prod/openai",
            },
        )

        receipts_table.grant_read_write_data(receipts_lambda)
        sessions_table.grant_read_data(receipts_lambda)
        receipt_images_bucket.grant_read_write(receipts_lambda)
        openai_secret.grant_read(receipts_lambda)

        receipts_integration = HttpLambdaIntegration(
            "ReceiptsLambdaIntegration",
            receipts_lambda,
        )

        http_api = HttpApi(
            self,
            "ReceiptProcessingApi",
            cors_preflight={
                "allow_origins": ["*"],
                "allow_methods": [
                    CorsHttpMethod.GET,
                    CorsHttpMethod.POST,
                    CorsHttpMethod.DELETE,
                    CorsHttpMethod.OPTIONS,
                ],
                "allow_headers": [
                    "Content-Type",
                    "Authorization",
                ],
            },
        )

        http_api.add_routes(
            path="/receipts/upload",
            methods=[HttpMethod.POST],
            integration=receipts_integration,
        )

        http_api.add_routes(
            path="/receipts",
            methods=[HttpMethod.GET],
            integration=receipts_integration,
        )

        http_api.add_routes(
            path="/receipts/{receiptId}",
            methods=[HttpMethod.GET],
            integration=receipts_integration,
        )

        http_api.add_routes(
            path="/receipts/{receiptId}",
            methods=[HttpMethod.DELETE],
            integration=receipts_integration,
        )
        
        http_api.add_routes(
            path="/receipts/process/{receiptId}",
            methods=[HttpMethod.POST],
            integration=receipts_integration,
        )

        CfnOutput(
            self,
            "ApiUrl",
            value=http_api.api_endpoint,
        )