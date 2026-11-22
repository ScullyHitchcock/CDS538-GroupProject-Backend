import os
import time

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

availability = boto3.resource("dynamodb").Table(os.environ["STORE_AVAILABILITY_TABLE_NAME"])
dynamodb = boto3.client("dynamodb")
serializer = TypeSerializer()


def sold_out_product_ids(store_id):
    result = availability.get_item(Key={"storeId": store_id}, ConsistentRead=True)
    return result.get("Item", {}).get("soldOutProductIds", set())


def set_sold_out(store_id, product_id, sold_out):
    operation = "ADD" if sold_out else "DELETE"
    retry_on_transaction_conflict(
        availability.update_item,
        Key={"storeId": store_id},
        UpdateExpression=f"{operation} soldOutProductIds :product",
        ExpressionAttributeValues={":product": {product_id}},
    )


def write_available_order(orders_table, order, product_ids):
    products = {f":product{index}": {"S": product_id} for index, product_id in enumerate(product_ids)}
    in_stock = " AND ".join(f"NOT contains(soldOutProductIds, {value})" for value in products)

    retry_on_transaction_conflict(
        dynamodb.transact_write_items,
        TransactItems=[
            {"ConditionCheck": {
                "TableName": availability.name,
                "Key": {"storeId": {"S": order["storeId"]}},
                "ConditionExpression": f"attribute_not_exists(soldOutProductIds) OR ({in_stock})",
                "ExpressionAttributeValues": products,
                "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
            }},
            {"Put": {
                "TableName": orders_table,
                "Item": {name: serializer.serialize(value) for name, value in order.items()},
                "ConditionExpression": "attribute_not_exists(orderId)",
            }},
        ],
    )


def sold_out_at_checkout(error, store_id):
    reasons = error.response.get("CancellationReasons", [])
    if not reasons or reasons[0]["Code"] != "ConditionalCheckFailed":
        return set()

    previous_state = reasons[0].get("Item")
    if previous_state is None:
        return sold_out_product_ids(store_id)
    return set(previous_state["soldOutProductIds"]["SS"])


def retry_on_transaction_conflict(operation, **options):
    for delay in (0.05, 0.1, None):
        try:
            return operation(**options)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            reasons = error.response.get("CancellationReasons", [])
            conflicted = code == "TransactionConflictException" or any(
                reason["Code"] == "TransactionConflict" for reason in reasons
            )
            if delay is None or not conflicted:
                raise
            time.sleep(delay)
