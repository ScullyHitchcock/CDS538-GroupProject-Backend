MAX_ITEM_QUANTITY = 99


def cart_line_key(item, topping_ids):
    options = item.get("options", {})
    return item["productId"], options.get("ice"), options.get("sugar"), tuple(sorted(topping_ids))


def parse_cart(items):
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one product")

    cart = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("invalid order item")
        product_id = entry.get("productId")
        quantity = entry.get("quantity")
        if not isinstance(product_id, str) or not product_id:
            raise ValueError("unknown product")
        if type(quantity) is not int or quantity < 1:
            raise ValueError("invalid product quantity")
        if "options" in entry:
            options = entry["options"]
            if not isinstance(options, dict) or set(options) != {"ice", "sugar"} or any(
                not isinstance(value, str) for value in options.values()
            ):
                raise ValueError("select both ice and sugar levels")

        topping_ids = entry.get("toppingIds", [])
        if not isinstance(topping_ids, list) or any(
            not isinstance(topping_id, str) or not topping_id for topping_id in topping_ids
        ):
            raise ValueError("invalid toppings")
        if len(topping_ids) != len(set(topping_ids)):
            raise ValueError("select each topping at most once per drink")

        key = cart_line_key(entry, topping_ids)
        if key in cart:
            raise ValueError("list each product and option combination once")
        cart[key] = quantity
    return cart


def build_order_items(cart, drinks, toppings):
    items = []
    quantities = {}
    for (product_id, ice, sugar, topping_ids), quantity in cart.items():
        drink = drinks.get(product_id)
        product = drink or toppings.get(product_id)
        if product is None:
            raise ValueError("unknown product")
        quantities[product_id] = quantities.get(product_id, 0) + quantity
        if quantities[product_id] > MAX_ITEM_QUANTITY:
            raise ValueError("invalid product quantity")

        item = {
            "productId": product_id,
            "name": product["name"],
            "quantity": quantity,
            "unitPriceCents": product["priceCents"],
        }
        if drink is None:
            if ice is not None or topping_ids:
                raise ValueError("standalone toppings cannot have drink options or toppings")
            item["unit"] = "portion"
        elif ice is not None:
            if ice not in drink["options"]["ice"]["choices"] or sugar not in drink["options"]["sugar"]["choices"]:
                raise ValueError("select valid ice and sugar levels for this drink")
            item["options"] = {"ice": ice, "sugar": sugar}

        if topping_ids:
            additions = []
            for topping_id in topping_ids:
                topping = toppings.get(topping_id)
                if topping is None:
                    raise ValueError("unknown topping")
                additions.append({
                    "productId": topping_id,
                    "name": topping["name"],
                    "unitPriceCents": topping["priceCents"],
                })
            item["toppings"] = additions
            item["unitPriceCents"] += sum(topping["unitPriceCents"] for topping in additions)
        items.append(item)
    total_amount_cents = sum(item["quantity"] * item["unitPriceCents"] for item in items)
    return items, total_amount_cents


def matches_saved_items(cart, items):
    saved_cart = {
        cart_line_key(item, [topping["productId"] for topping in item.get("toppings", [])]): item["quantity"]
        for item in items
    }
    return saved_cart == cart


def ordered_products(items):
    products = {}
    for item in items:
        products[item["productId"]] = item["name"]
        for topping in item.get("toppings", []):
            products[topping["productId"]] = topping["name"]
    return products
