import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

# 表名从环境变量读取，不写死在代码里（在 Lambda 控制台设置 TABLE_NAME）
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
cognito = boto3.client("cognito-idp")

MENU = {
    "pearl-milk-tea": {"name": "珍珠奶茶", "priceCents": 1600},
    "jasmine-milk-tea": {"name": "茉莉奶绿", "priceCents": 1500},
    "lemon-tea": {"name": "柠檬茶", "priceCents": 1300},
}
MAX_ITEM_QUANTITY = 99


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str, ensure_ascii=False),
    }


def has_group(claims, group):
    # 网关将此原型的 Cognito 组声明传成类似 "[customers]" 的字符串。
    group_claim = claims.get("cognito:groups")
    if not isinstance(group_claim, str) or not (
        group_claim.startswith("[") and group_claim.endswith("]")
    ):
        return False

    groups = [name.strip() for name in group_claim[1:-1].split(",")]
    return group in groups


def list_stores():
    pages = cognito.get_paginator("list_users_in_group").paginate(
        UserPoolId=os.environ["COGNITO_USER_POOL_ID"],
        GroupName="merchants",
    )
    stores = {}
    for page in pages:
        for user in page["Users"]:
            if not user["Enabled"]:
                continue
            sub = next((attr["Value"] for attr in user["Attributes"] if attr["Name"] == "sub"), None)
            if sub:
                stores[sub] = user["Username"]
    return stores


def query_orders(**options):
    orders = []
    while True:
        page = table.query(**options)
        orders.extend(page["Items"])
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return orders
        options["ExclusiveStartKey"] = last_key


def parse_cart(items):
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one drink and list each drink once")

    cart = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("invalid order item")
        product_id = entry.get("productId")
        quantity = entry.get("quantity")
        if not isinstance(product_id, str) or not product_id or product_id in cart:
            raise ValueError("unknown or duplicate drink")
        if type(quantity) is not int or quantity < 1:
            raise ValueError("invalid drink quantity")
        cart[product_id] = quantity
    return cart


def build_order_items(cart):
    items = []
    for product_id, quantity in cart.items():
        drink = MENU.get(product_id)
        if drink is None:
            raise ValueError("unknown drink")
        if quantity > MAX_ITEM_QUANTITY:
            raise ValueError("invalid drink quantity")
        items.append({
            "productId": product_id,
            "name": drink["name"],
            "quantity": quantity,
            "unitPriceCents": drink["priceCents"],
        })
    return items


def respond_to_saved_order(existing, order, cart):
    same_checkout = all(existing.get(field) == order[field] for field in ("customerId", "storeId", "note"))
    saved_cart = {item["productId"]: item["quantity"] for item in existing.get("items", [])}
    if not same_checkout or saved_cart != cart:
        return respond(409, {"error": "checkout ID is already in use"})
    return respond(200, existing)


def create_or_recover_cart_order(order, checkout_id, cart):
    # 同一顾客重试同一结算请求时，写入条件避免生成第二笔订单。
    order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{order["customerId"]}:{checkout_id}'))
    order["orderId"] = order_id
    lookup = {"Key": {"orderId": order_id}, "ConsistentRead": True}
    existing = table.get_item(**lookup).get("Item")
    if existing is not None:
        return respond_to_saved_order(existing, order, cart)

    if order["storeId"] not in list_stores():
        return respond(400, {"error": "select an available store"})
    try:
        items = build_order_items(cart)
    except ValueError as error:
        return respond(400, {"error": str(error)})
    order["items"] = items
    order["totalAmountCents"] = sum(item["quantity"] * item["unitPriceCents"] for item in items)
    try:
        table.put_item(Item=order, ConditionExpression="attribute_not_exists(orderId)")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        existing = table.get_item(**lookup).get("Item", {})
        return respond_to_saved_order(existing, order, cart)
    return respond(201, order)


def lambda_handler(event, context):
    route = event["routeKey"]

    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    user_id = claims["sub"]

    print(
        f"route={route} "
        f"requestId={context.aws_request_id} "
        f"userId={user_id}"
    )

    if route == "GET /stores":
        if not has_group(claims, "customers"):
            return respond(403, {"error": "only customers can list stores"})

        return respond(200, {
            "stores": [
                {"storeId": store_id, "name": name} for store_id, name in list_stores().items()
            ],
            "menu": [{"productId": product_id, **drink} for product_id, drink in MENU.items()],
            "maxItemQuantity": MAX_ITEM_QUANTITY,
        })

    if route == "POST /orders":
        if not has_group(claims, "customers"):
            return respond(403, {"error": "only customers can create orders"})

        try:
            data = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return respond(400, {"error": "invalid order body"})
        store_id = data.get("storeId") if isinstance(data, dict) else None
        if not isinstance(store_id, str) or not store_id:
            return respond(400, {"error": "select an available store"})

        order = {
            "customerId": user_id,
            "storeId": store_id,
            "status": "PENDING",
            "note": str(data.get("note", "")),
            "createdAt": int(time.time()),
        }
        if "items" not in data:
            # 兼容购物车上线前的前端和已有调用者。
            if store_id not in list_stores():
                return respond(400, {"error": "select an available store"})
            order["orderId"] = str(uuid.uuid4())
            table.put_item(Item=order)
            return respond(201, order)

        checkout_id = data.get("checkoutId")
        if not isinstance(checkout_id, str) or len(checkout_id) != 36:
            return respond(400, {"error": "invalid checkout ID"})
        try:
            checkout_uuid = uuid.UUID(checkout_id)
            if checkout_uuid.version != 4:
                return respond(400, {"error": "invalid checkout ID"})
            cart = parse_cart(data["items"])
        except ValueError as error:
            return respond(400, {"error": str(error)})
        return create_or_recover_cart_order(order, checkout_uuid, cart)

    if route == "GET /orders":
        if not has_group(claims, "customers"):
            return respond(403, {"error": "only customers can list their orders"})

        orders = query_orders(
            IndexName="CustomerOrdersIndex",
            KeyConditionExpression="customerId = :customer_id",
            ExpressionAttributeValues={":customer_id": user_id},
            ScanIndexForward=False,
        )
        return respond(200, {"orders": orders})

    if route == "GET /merchant/orders":
        if not has_group(claims, "merchants"):
            return respond(403, {"error": "only merchants can list pending orders"})

        orders = query_orders(
            IndexName="StoreOrdersIndex",
            KeyConditionExpression="storeId = :store_id AND #status = :pending",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":store_id": user_id, ":pending": "PENDING"},
        )
        orders.sort(key=lambda order: order["createdAt"])
        return respond(200, {"orders": orders})

    if route == "PATCH /merchant/orders/{id}/ready":
        if not has_group(claims, "merchants"):
            return respond(403, {"error": "only merchants can mark orders ready"})

        order_id = event["pathParameters"]["id"]
        try:
            table.update_item(
                Key={"orderId": order_id},
                UpdateExpression="SET #status = :ready",
                ConditionExpression="storeId = :store_id AND #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":store_id": user_id,
                    ":pending": "PENDING",
                    ":ready": "READY",
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return respond(409, {"error": "order is not pending at your store"})

        return respond(200, {"orderId": order_id, "status": "READY"})

    if route == "GET /orders/{id}":
        order_id = event["pathParameters"]["id"]
        item = table.get_item(Key={"orderId": order_id}).get("Item")
        if item is None:
            return respond(404, {"error": "order not found"})
        if item.get("customerId") != user_id:
            return respond(403, {"error": "access denied"})
        return respond(200, item)

    return respond(404, {"error": "no such route"})
