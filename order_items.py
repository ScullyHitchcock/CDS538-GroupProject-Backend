MAX_ITEM_QUANTITY = 99


def cart_line_key(item):
    options = item.get("options", {})
    return item["productId"], options.get("ice"), options.get("sugar")


def parse_cart(items):
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one drink")

    cart = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("invalid order item")
        product_id = entry.get("productId")
        quantity = entry.get("quantity")
        if not isinstance(product_id, str) or not product_id:
            raise ValueError("unknown drink")
        if type(quantity) is not int or quantity < 1:
            raise ValueError("invalid drink quantity")
        if "options" in entry:
            options = entry["options"]
            if not isinstance(options, dict) or set(options) != {"ice", "sugar"} or any(
                not isinstance(value, str) for value in options.values()
            ):
                raise ValueError("select both ice and sugar levels")

        key = cart_line_key(entry)
        if key in cart:
            raise ValueError("list each drink and option combination once")
        cart[key] = quantity
    return cart


def build_order_items(cart, menu):
    items = []
    quantities = {}
    for (product_id, ice, sugar), quantity in cart.items():
        drink = menu.get(product_id)
        if drink is None:
            raise ValueError("unknown drink")
        quantities[product_id] = quantities.get(product_id, 0) + quantity
        if quantities[product_id] > MAX_ITEM_QUANTITY:
            raise ValueError("invalid drink quantity")

        item = {
            "productId": product_id,
            "name": drink["name"],
            "quantity": quantity,
            "unitPriceCents": drink["priceCents"],
        }
        if ice is not None:
            if ice not in drink["options"]["ice"]["choices"] or sugar not in drink["options"]["sugar"]["choices"]:
                raise ValueError("select valid ice and sugar levels for this drink")
            item["options"] = {"ice": ice, "sugar": sugar}
        items.append(item)
    total_amount_cents = sum(item["quantity"] * item["unitPriceCents"] for item in items)
    return items, total_amount_cents


def matches_saved_items(cart, items):
    saved_cart = {cart_line_key(item): item["quantity"] for item in items}
    return saved_cart == cart
