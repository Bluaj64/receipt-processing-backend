#!/usr/bin/env python3

import aws_cdk as cdk

from infra.receipts_stack import ReceiptsStack

app = cdk.App()

ReceiptsStack(
    app,
    "ReceiptProcessingStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region="us-east-2",
    ),
)

app.synth()