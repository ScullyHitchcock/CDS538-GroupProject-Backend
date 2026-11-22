import json
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

from order_items import (
    MAX_ITEM_QUANTITY,
    build_order_items,
    matches_saved_items,
    ordered_products,
    parse_cart,
)
from store_availability import set_sold_out, sold_out_at_checkout, sold_out_product_ids, write_available_order

# 表名从环境变量读取，不写死在代码里（在 Lambda 控制台设置 TABLE_NAME）
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
cognito = boto3.client("cognito-idp")

STANDARD_DRINK_OPTIONS = {
    "ice": {"choices": ["正常冰", "少冰", "去冰"], "default": "正常冰"},
    "sugar": {"choices": ["正常糖", "半糖", "无糖"], "default": "正常糖"},
}
MENU = {
    "pearl-milk-tea": {"name": "珍珠奶茶", "priceCents": 1600, "options": STANDARD_DRINK_OPTIONS},
    "jasmine-milk-tea": {"name": "茉莉奶绿", "priceCents": 1500, "options": STANDARD_DRINK_OPTIONS},
    "lemon-tea": {"name": "柠檬茶", "priceCents": 1300, "options": STANDARD_DRINK_OPTIONS},
}

TOPPINGS = {
    "topping-pearl": {"name": "珍珠", "priceCents": 300},
    "topping-coconut": {"name": "椰果", "priceCents": 300},
}
PRODUCTS = MENU | TOPPINGS


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


def catalog_products(products):
    return [{"productId": product_id, **product} for product_id, product in products.items()]


def store_menu(store_id, include_sold_out=False):
    sold_out = sold_out_product_ids(store_id)
    drinks = [
        {**drink, "soldOut": drink["productId"] in sold_out}
        for drink in catalog_products(MENU)
    ]
    return {
        "menu": drinks if include_sold_out else [drink for drink in drinks if not drink["soldOut"]],
        "toppings": [
            {**topping, "soldOut": topping["productId"] in sold_out}
            for topping in catalog_products(TOPPINGS)
        ],
    }


def update_merchant_menu(store_id, product_id, body):
    product = PRODUCTS.get(product_id)
    if product is None:
        return respond(404, {"error": "unknown product"})
    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "invalid menu update"})
    sold_out = data.get("soldOut") if isinstance(data, dict) else None
    if type(sold_out) is not bool:
        return respond(400, {"error": "soldOut must be a boolean"})

    set_sold_out(store_id, product_id, sold_out)
    return respond(200, {"productId": product_id, **product, "soldOut": sold_out})


def query_orders(**options):
    orders = []
    while True:
        page = table.query(**options)
        orders.extend(page["Items"])
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return orders
        options["ExclusiveStartKey"] = last_key


def respond_to_saved_order(existing, order, cart):
    same_checkout = all(existing.get(field) == order[field] for field in ("customerId", "storeId", "note"))
    if not same_checkout or not matches_saved_items(cart, existing.get("items", [])):
        return respond(409, {
            "code": "CHECKOUT_ID_CONFLICT",
            "error": "checkout ID is already in use",
        })
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
        items, total_amount_cents = build_order_items(cart, MENU, TOPPINGS)
    except ValueError as error:
        return respond(400, {"error": str(error)})
    order["items"] = items
    order["totalAmountCents"] = total_amount_cents
    products = ordered_products(items)
    try:
        write_available_order(table.name, order, products)
    except ClientError as error:
        if error.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        existing = table.get_item(**lookup).get("Item")
        if existing is not None:
            return respond_to_saved_order(existing, order, cart)
        sold_out = sold_out_at_checkout(error, order["storeId"])
        unavailable = [
            {"productId": product_id, "name": name}
            for product_id, name in products.items()
            if product_id in sold_out
        ]
        if not unavailable:
            raise
        return respond(409, {"code": "ITEMS_SOLD_OUT", "items": unavailable})
    return respond(201, order)


def create_order(body, customer_id):
    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "invalid order body"})
    store_id = data.get("storeId") if isinstance(data, dict) else None
    if not isinstance(store_id, str) or not store_id:
        return respond(400, {"error": "select an available store"})

    order = {
        "customerId": customer_id,
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
            "menu": catalog_products(MENU),
            "toppings": catalog_products(TOPPINGS),
            "maxItemQuantity": MAX_ITEM_QUANTITY,
        })

    if route == "GET /stores/{storeId}/menu":
        if not has_group(claims, "customers"):
            return respond(403, {"error": "only customers can view store menus"})

        store_id = event["pathParameters"]["storeId"]
        if store_id not in list_stores():
            return respond(404, {"error": "store not found"})
        return respond(200, store_menu(store_id))

    if route in ("GET /merchant/menu", "PATCH /merchant/menu/{productId}"):
        if not has_group(claims, "merchants"):
            return respond(403, {"error": "only merchants can manage store menus"})

        if route == "GET /merchant/menu":
            return respond(200, store_menu(user_id, include_sold_out=True))
        return update_merchant_menu(user_id, event["pathParameters"]["productId"], event.get("body"))

    if route == "POST /orders":
        if not has_group(claims, "customers"):
            return respond(403, {"error": "only customers can create orders"})

        return create_order(event.get("body"), user_id)

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
