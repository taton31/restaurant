from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from headers import emulator

load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


APP_TITLE = "Restaurant Ranker"
DATABASE_URL = required_env("DATABASE_URL")
YANDEX_LAYOUT_URL = required_env("YANDEX_LAYOUT_URL")
YANDEX_MENU_URL_TEMPLATE = required_env("YANDEX_MENU_URL_TEMPLATE")
YANDEX_REFERER = required_env("YANDEX_REFERER")
YANDEX_LATITUDE = float(required_env("YANDEX_LATITUDE"))
YANDEX_LONGITUDE = float(required_env("YANDEX_LONGITUDE"))
YANDEX_USER_AGENT = required_env("YANDEX_USER_AGENT")
YANDEX_APP_VERSION = required_env("YANDEX_APP_VERSION")
SYNC_HOUR_UTC = int(os.getenv("SYNC_HOUR_UTC", "3"))
AUTO_SYNC_MENUS = os.getenv("AUTO_SYNC_MENUS", "1").strip().lower() in {"1", "true", "yes"}

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Restaurant(Base):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    business: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rating_text: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rating_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rating_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    logo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    dishes: Mapped[list["Dish"]] = relationship(back_populates="restaurant", cascade="all, delete-orphan")
    votes: Mapped[list["RestaurantVote"]] = relationship(back_populates="restaurant", cascade="all, delete-orphan")
    comments: Mapped[list["RestaurantComment"]] = relationship(
        back_populates="restaurant",
        cascade="all, delete-orphan",
        order_by="desc(RestaurantComment.created_at)",
    )


class Dish(Base):
    __tablename__ = "dishes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id", ondelete="CASCADE"), index=True)
    source_key: Mapped[str] = mapped_column(String(255), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rating_text: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rating_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    restaurant: Mapped["Restaurant"] = relationship(back_populates="dishes")
    votes: Mapped[list["DishVote"]] = relationship(back_populates="dish", cascade="all, delete-orphan")


class RestaurantVote(Base):
    __tablename__ = "restaurant_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id", ondelete="CASCADE"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    restaurant: Mapped["Restaurant"] = relationship(back_populates="votes")


class RestaurantComment(Base):
    __tablename__ = "restaurant_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    restaurant: Mapped["Restaurant"] = relationship(back_populates="comments")


class DishVote(Base):
    __tablename__ = "dish_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dish_id: Mapped[int] = mapped_column(ForeignKey("dishes.id", ondelete="CASCADE"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    dish: Mapped["Dish"] = relationship(back_populates="votes")


Base.metadata.create_all(bind=engine)

scheduler = AsyncIOScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        sync_data(refresh_menus=False)
    except Exception as exc:
        print(f"[startup] initial sync failed: {exc}")

    if not scheduler.running:
        scheduler.add_job(sync_data, "cron", hour=SYNC_HOUR_UTC, minute=0, id="daily_sync", replace_existing=True)
        scheduler.start()

    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


app = FastAPI(title=APP_TITLE, lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


def display_score(score: Optional[int]) -> str:
    return str(score) if score is not None else "-"


def display_source_score(restaurant: Restaurant) -> str:
    if restaurant.rating_text:
        return restaurant.rating_text
    if restaurant.rating_value is not None:
        return f"{restaurant.rating_value:.1f}"
    return "-"


def restaurant_image(restaurant: Restaurant) -> str:
    return restaurant.image_url or restaurant.logo_url or "https://placehold.co/700x700?text=Restaurant"


def dish_image(dish: Dish) -> str:
    return dish.image_url or "https://placehold.co/700x700?text=Dish"


templates.env.globals.update(
    display_score=display_score,
    display_source_score=display_source_score,
    restaurant_image=restaurant_image,
    dish_image=dish_image,
    score_values=range(10, 0, -1),
)


def safe_redirect_from_referer(request: Request, fallback: str = "/") -> str:
    referer = request.headers.get("referer")
    if not referer:
        return fallback

    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return fallback

    target = parsed.path or fallback
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return target


def _first_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        if isinstance(value.get("value"), str):
            return value["value"]
        if isinstance(value.get("text"), str):
            return value["text"]
        if "text" in value:
            return _first_str(value["text"])
    return None


def _walk(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9а-яё]+", "-", value, flags=re.IGNORECASE)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "restaurant"


def _normalize_image_url(value: Any, width: int = 700, height: int = 700) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None

    url = value.strip()
    url = url.replace("{w}", str(width)).replace("{h}", str(height))

    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://eda.yandex.ru" + url
    if url.startswith("https://eda.yandex/"):
        return url.replace("https://eda.yandex/", "https://eda.yandex.ru/")
    if url.startswith("http://eda.yandex/"):
        return url.replace("http://eda.yandex/", "https://eda.yandex.ru/")
    return url


def _extract_rating_text(node: dict[str, Any]) -> tuple[Optional[str], Optional[float], Optional[int]]:
    rating = (
        node.get("features", {}).get("rating")
        or node.get("rating")
        or node.get("data", {}).get("features", {}).get("rating")
    )
    if not isinstance(rating, dict):
        return None, None, None

    rating_text = _first_str(rating.get("text"))
    rating_value = None
    rating_count = None

    if rating_text:
        value_match = re.match(r"\s*([0-9]+(?:[.,][0-9]+)?)", rating_text)
        if value_match:
            rating_value = float(value_match.group(1).replace(",", "."))

        count_match = re.search(r"\((\d+)\+?\)", rating_text)
        if count_match:
            rating_count = int(count_match.group(1))

    return rating_text, rating_value, rating_count


def _extract_image(node: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    image_url = None
    logo_url = None

    picture = node.get("picture")
    if isinstance(picture, dict):
        image_url = _normalize_image_url(picture.get("image") or picture.get("url"))

    media = node.get("media")
    if isinstance(media, dict):
        photos = media.get("photos")
        if isinstance(photos, list):
            for photo in photos:
                if isinstance(photo, dict):
                    image_url = image_url or _normalize_image_url(photo.get("uri") or photo.get("url"))
                    if image_url:
                        break

    for features in (node.get("features"), node.get("data", {}).get("features")):
        if not isinstance(features, dict):
            continue
        logo = features.get("logo")
        if not isinstance(logo, list):
            continue
        for theme in logo:
            values = theme.get("value") if isinstance(theme, dict) else None
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    logo_url = logo_url or _normalize_image_url(item.get("logo_url") or item.get("url"))
                    if logo_url:
                        break

    return image_url, logo_url


def _looks_like_restaurant(node: dict[str, Any]) -> bool:
    brand = node.get("brand")
    return isinstance(brand, dict) and brand.get("business") == "restaurant"


def _normalize_restaurant(node: dict[str, Any], index: int) -> dict[str, Any]:
    brand = node.get("brand") if isinstance(node.get("brand"), dict) else {}
    name = _first_str(node.get("name")) or _first_str(node.get("title")) or _first_str(brand.get("name"))
    name = name or f"Restaurant {index + 1}"

    slug = node.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        slug = _slugify(name)

    brand_slug = brand.get("slug") if isinstance(brand.get("slug"), str) else None
    rating_text, rating_value, rating_count = _extract_rating_text(node)
    image_url, logo_url = _extract_image(node)

    external_id = None
    for key in ("id", "place_id", "placeId", "restaurant_id"):
        if isinstance(node.get(key), (str, int)):
            external_id = str(node[key])
            break

    return {
        "source_key": f"yandex:{slug}",
        "external_id": external_id or brand_slug,
        "slug": slug,
        "name": name,
        "business": "restaurant",
        "rating_text": rating_text,
        "rating_value": rating_value,
        "rating_count": rating_count,
        "image_url": image_url,
        "logo_url": logo_url,
        "raw_json": node,
    }


def extract_restaurants_from_payload(payload: Any) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    index = 0

    for node in _walk(payload):
        if not _looks_like_restaurant(node):
            continue

        normalized = _normalize_restaurant(node, index)
        key = normalized["slug"]
        existing = candidates.get(key)
        if not existing:
            candidates[key] = normalized
        else:
            score_new = sum(1 for k in ("rating_text", "rating_value", "image_url", "logo_url") if normalized.get(k))
            score_old = sum(1 for k in ("rating_text", "rating_value", "image_url", "logo_url") if existing.get(k))
            if score_new > score_old:
                candidates[key] = normalized
        index += 1

    return list(candidates.values())


def _yandex_headers(referer: Optional[str] = None) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ru",
        "cache-control": "no-cache",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://eda.yandex.ru",
        "pragma": "no-cache",
        "referer": referer or YANDEX_REFERER,
        "user-agent": YANDEX_USER_AGENT,
        "x-app-version": YANDEX_APP_VERSION,
        "x-client-session": emulator.generate_id(),
        "x-device-id": emulator.generate_id(),
        "x-platform": "desktop_web",
        "x-retpath-y": YANDEX_REFERER,
        "x-taxi": f"{YANDEX_USER_AGENT} platform=eats_desktop_web",
        "x-ya-client-time": now,
        "x-ya-coordinates": f"latitude={YANDEX_LATITUDE},longitude={YANDEX_LONGITUDE}",
    }


def _load_payload_from_yandex() -> Any:
    request_body = {
        "location": {
            "latitude": YANDEX_LATITUDE,
            "longitude": YANDEX_LONGITUDE,
        },
    }
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.post(YANDEX_LAYOUT_URL, headers=_yandex_headers(), json=request_body)
        response.raise_for_status()
        return response.json()


def _iter_menu_categories(categories: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(categories, list):
        return
    for category in categories:
        if not isinstance(category, dict):
            continue
        yield category
        yield from _iter_menu_categories(category.get("categories"))


def _normalize_price(item: dict[str, Any]) -> Optional[str]:
    value = item.get("decimalPrice") or item.get("price")
    if value is None:
        return None
    value_str = str(value).strip()
    return f"{value_str} ₽" if value_str else None


def _normalize_dish(item: dict[str, Any], category_name: Optional[str], index: int) -> dict[str, Any]:
    name = _first_str(item.get("name")) or _first_str(item.get("title")) or f"Dish {index + 1}"
    picture = item.get("picture")
    image_url = None
    if isinstance(picture, dict):
        image_url = _normalize_image_url(picture.get("uri") or picture.get("image") or picture.get("url"), 700, 700)

    description = _first_str(item.get("description"))
    weight = _first_str(item.get("weight"))
    if weight:
        description = f"{description or ''} · {weight}".strip(" ·")

    rating_text, rating_value, _ = _extract_rating_text(item)
    external_id = item.get("id") or item.get("publicId") or index

    return {
        "external_id": str(external_id),
        "name": name,
        "description": description,
        "price": _normalize_price(item),
        "image_url": image_url,
        "rating_text": rating_text or category_name,
        "rating_value": rating_value,
        "raw_json": item,
    }


def fetch_menu_for_restaurant(restaurant: Restaurant) -> list[dict[str, Any]]:
    url = YANDEX_MENU_URL_TEMPLATE.format(slug=restaurant.slug, external_id=restaurant.external_id or "")
    referer = f"{YANDEX_REFERER.rstrip('/')}/r/{restaurant.slug}"

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(url, headers=_yandex_headers(referer=referer))
        response.raise_for_status()
        payload = response.json()

    categories = payload.get("payload", {}).get("categories") if isinstance(payload, dict) else None
    if categories is None and isinstance(payload, dict):
        categories = payload.get("categories")

    dishes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category in _iter_menu_categories(categories):
        category_name = _first_str(category.get("name"))
        items = category.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("available") is False:
                continue
            external_id = str(item.get("id") or item.get("publicId") or f"{category_name}:{len(dishes)}")
            if external_id in seen:
                continue
            seen.add(external_id)
            dishes.append(_normalize_dish(item, category_name, len(dishes)))

    return dishes


def refresh_menu(restaurant_id: int) -> int:
    with SessionLocal() as session:
        restaurant = session.execute(select(Restaurant).where(Restaurant.id == restaurant_id)).scalar_one_or_none()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")

        items = fetch_menu_for_restaurant(restaurant)
        session.query(Dish).filter(Dish.restaurant_id == restaurant.id).delete()
        for i, item in enumerate(items):
            session.add(
                Dish(
                    restaurant_id=restaurant.id,
                    source_key=f"{restaurant.slug}:{item.get('external_id') or i}",
                    external_id=item.get("external_id"),
                    name=item["name"],
                    description=item.get("description"),
                    price=item.get("price"),
                    image_url=item.get("image_url"),
                    rating_text=item.get("rating_text"),
                    rating_value=item.get("rating_value"),
                    raw_json=item.get("raw_json", {}),
                )
            )
        session.commit()
        return len(items)


def sync_data(refresh_menus: bool = AUTO_SYNC_MENUS) -> dict[str, int]:
    payload = _load_payload_from_yandex()
    restaurants = extract_restaurants_from_payload(payload)

    with SessionLocal() as session:
        upserted = 0
        restaurant_ids: list[int] = []

        for item in restaurants:
            restaurant = session.execute(select(Restaurant).where(Restaurant.slug == item["slug"])).scalar_one_or_none()
            if restaurant is None:
                restaurant = Restaurant(**item)
                session.add(restaurant)
            else:
                for key, value in item.items():
                    setattr(restaurant, key, value)
            upserted += 1
            session.flush()
            restaurant_ids.append(restaurant.id)

        for stale in session.execute(select(Restaurant).where(Restaurant.business != "restaurant")).scalars().all():
            session.delete(stale)

        session.commit()

    refreshed_dishes = 0
    if refresh_menus:
        for restaurant_id in restaurant_ids:
            try:
                refreshed_dishes += refresh_menu(restaurant_id)
            except Exception as exc:
                print(f"[sync] menu refresh failed for restaurant_id={restaurant_id}: {exc}")

    return {"restaurants": upserted, "dishes": refreshed_dishes}


def get_restaurant_stats(session, restaurant_id: int) -> dict[str, Any]:
    vote = session.execute(
        select(RestaurantVote)
        .where(RestaurantVote.restaurant_id == restaurant_id)
        .order_by(RestaurantVote.created_at.desc(), RestaurantVote.id.desc())
    ).scalars().first()
    dish_count = session.execute(select(func.count(Dish.id)).where(Dish.restaurant_id == restaurant_id)).scalar_one()
    comment_count = session.execute(
        select(func.count(RestaurantComment.id)).where(RestaurantComment.restaurant_id == restaurant_id)
    ).scalar_one()

    return {
        "score": int(vote.score) if vote is not None else None,
        "dish_count": int(dish_count or 0),
        "comment_count": int(comment_count or 0),
    }


def get_dish_stats(session, dish_id: int) -> dict[str, Any]:
    vote = session.execute(
        select(DishVote).where(DishVote.dish_id == dish_id).order_by(DishVote.created_at.desc(), DishVote.id.desc())
    ).scalars().first()
    return {
        "score": int(vote.score) if vote is not None else None,
    }


def set_restaurant_score(session, restaurant_id: int, score: int) -> None:
    votes = session.execute(
        select(RestaurantVote)
        .where(RestaurantVote.restaurant_id == restaurant_id)
        .order_by(RestaurantVote.created_at.desc(), RestaurantVote.id.desc())
    ).scalars().all()

    if votes:
        vote = votes[0]
        vote.score = score
        vote.created_at = now_utc()
        for duplicate in votes[1:]:
            session.delete(duplicate)
    else:
        session.add(RestaurantVote(restaurant_id=restaurant_id, score=score))


def set_dish_score(session, dish_id: int, score: int) -> None:
    votes = session.execute(
        select(DishVote).where(DishVote.dish_id == dish_id).order_by(DishVote.created_at.desc(), DishVote.id.desc())
    ).scalars().all()

    if votes:
        vote = votes[0]
        vote.score = score
        vote.created_at = now_utc()
        for duplicate in votes[1:]:
            session.delete(duplicate)
    else:
        session.add(DishVote(dish_id=dish_id, score=score))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", sort: str = "source_rating", min_my_rating: str = ""):
    allowed_sorts = {"source_rating", "my_rating", "name"}
    if sort not in allowed_sorts:
        sort = "source_rating"

    min_rating_value: Optional[int] = None
    if min_my_rating.strip():
        try:
            min_rating_value = max(1, min(10, int(min_my_rating)))
        except ValueError:
            min_rating_value = None

    with SessionLocal() as session:
        query = select(Restaurant).where(Restaurant.business == "restaurant")
        if q.strip():
            like = f"%{q.strip().lower()}%"
            query = query.where(func.lower(Restaurant.name).like(like) | func.lower(Restaurant.slug).like(like))
        restaurants = session.execute(query).scalars().all()

        rows = []
        for restaurant in restaurants:
            stats = get_restaurant_stats(session, restaurant.id)
            if min_rating_value is not None and (stats["score"] is None or stats["score"] < min_rating_value):
                continue
            rows.append({"restaurant": restaurant, "stats": stats})

        if sort == "name":
            rows.sort(key=lambda item: item["restaurant"].name.lower())
        elif sort == "my_rating":
            rows.sort(
                key=lambda item: (
                    item["stats"]["score"] is None,
                    -(item["stats"]["score"] or 0),
                    item["restaurant"].name.lower(),
                )
            )
        else:
            rows.sort(
                key=lambda item: (
                    item["restaurant"].rating_value is None,
                    -(item["restaurant"].rating_value or 0),
                    item["restaurant"].name.lower(),
                )
            )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "title": "Рестораны",
            "rows": rows,
            "q": q,
            "sort": sort,
            "min_rating_value": min_rating_value,
        },
    )


@app.post("/sync")
async def sync_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(sync_data, False)
    return RedirectResponse(url="/", status_code=303)


@app.get("/restaurant/{restaurant_id}", response_class=HTMLResponse)
def restaurant_detail_by_id(request: Request, restaurant_id: int, q: str = ""):
    with SessionLocal() as session:
        slug = session.execute(select(Restaurant.slug).where(Restaurant.id == restaurant_id)).scalar_one_or_none()
        if slug is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")
    return restaurant_detail(request=request, slug=slug, q=q)


@app.post("/restaurant/{restaurant_id}/vote")
async def vote_restaurant_by_id(request: Request, restaurant_id: int, score: int = Form(...)):
    if score < 1 or score > 10:
        raise HTTPException(status_code=400, detail="Score must be between 1 and 10")
    with SessionLocal() as session:
        restaurant = session.execute(select(Restaurant).where(Restaurant.id == restaurant_id)).scalar_one_or_none()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")
        set_restaurant_score(session, restaurant.id, score)
        session.commit()
    return RedirectResponse(url=safe_redirect_from_referer(request), status_code=303)


@app.post("/restaurants/{slug}/refresh-menu")
async def refresh_restaurant_menu(slug: str):
    with SessionLocal() as session:
        restaurant = session.execute(select(Restaurant).where(Restaurant.slug == slug)).scalar_one_or_none()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")
        restaurant_id = restaurant.id
    refresh_menu(restaurant_id)
    return RedirectResponse(url=f"/restaurants/{slug}", status_code=303)


@app.get("/restaurants/{slug}", response_class=HTMLResponse)
def restaurant_detail(request: Request, slug: str, q: str = ""):
    with SessionLocal() as session:
        restaurant = session.execute(select(Restaurant).where(Restaurant.slug == slug)).scalar_one_or_none()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")

        if not session.execute(select(func.count(Dish.id)).where(Dish.restaurant_id == restaurant.id)).scalar_one():
            try:
                refresh_menu(restaurant.id)
            except Exception as exc:
                print(f"[menu] refresh failed for {slug}: {exc}")
                session.rollback()

        restaurant = session.execute(select(Restaurant).where(Restaurant.slug == slug)).scalar_one()
        dishes_query = select(Dish).where(Dish.restaurant_id == restaurant.id)
        if q.strip():
            like = f"%{q.strip()}%"
            dishes_query = dishes_query.where(Dish.name.ilike(like) | Dish.description.ilike(like))
        dishes = session.execute(dishes_query).scalars().all()

        rest_stats = get_restaurant_stats(session, restaurant.id)
        comments = session.execute(
            select(RestaurantComment)
            .where(RestaurantComment.restaurant_id == restaurant.id)
            .order_by(RestaurantComment.created_at.desc())
            .limit(20)
        ).scalars().all()
        dish_rows = [{"dish": dish, "stats": get_dish_stats(session, dish.id)} for dish in dishes]

    return templates.TemplateResponse(
        request,
        "restaurant_detail.html",
        {
            "title": restaurant.name,
            "restaurant": restaurant,
            "rest_stats": rest_stats,
            "comments": comments,
            "dish_rows": dish_rows,
            "q": q,
        },
    )


@app.post("/restaurants/{slug}/vote")
async def vote_restaurant(slug: str, score: int = Form(...)):
    if score < 1 or score > 10:
        raise HTTPException(status_code=400, detail="Score must be between 1 and 10")
    with SessionLocal() as session:
        restaurant = session.execute(select(Restaurant).where(Restaurant.slug == slug)).scalar_one_or_none()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")
        set_restaurant_score(session, restaurant.id, score)
        session.commit()
    return RedirectResponse(url=f"/restaurants/{slug}", status_code=303)


@app.post("/restaurants/{slug}/comment")
async def comment_restaurant(slug: str, text: str = Form(...)):
    comment = text.strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Comment is empty")
    if len(comment) > 2000:
        raise HTTPException(status_code=400, detail="Comment is too long")

    with SessionLocal() as session:
        restaurant = session.execute(select(Restaurant).where(Restaurant.slug == slug)).scalar_one_or_none()
        if restaurant is None:
            raise HTTPException(status_code=404, detail="Restaurant not found")
        session.add(RestaurantComment(restaurant_id=restaurant.id, text=comment))
        session.commit()
    return RedirectResponse(url=f"/restaurants/{slug}", status_code=303)


@app.post("/dishes/{dish_id}/vote")
async def vote_dish(dish_id: int, score: int = Form(...)):
    if score < 1 or score > 10:
        raise HTTPException(status_code=400, detail="Score must be between 1 and 10")
    with SessionLocal() as session:
        dish = session.execute(select(Dish).where(Dish.id == dish_id)).scalar_one_or_none()
        if dish is None:
            raise HTTPException(status_code=404, detail="Dish not found")
        restaurant = session.execute(select(Restaurant).where(Restaurant.id == dish.restaurant_id)).scalar_one()
        restaurant_slug = restaurant.slug
        set_dish_score(session, dish.id, score)
        session.commit()
    return RedirectResponse(url=f"/restaurants/{restaurant_slug}", status_code=303)


@app.post("/admin/resync")
def resync_now(refresh_menus: bool = False):
    result = sync_data(refresh_menus=refresh_menus)
    return {"ok": True, **result}


@app.get("/admin/export")
def export_db():
    with SessionLocal() as session:
        restaurants = session.execute(select(Restaurant).where(Restaurant.business == "restaurant")).scalars().all()
        return {
            "restaurants": [
                {
                    "id": restaurant.id,
                    "slug": restaurant.slug,
                    "name": restaurant.name,
                    "rating": restaurant.rating_text,
                    "image_url": restaurant.image_url,
                    "dishes": get_restaurant_stats(session, restaurant.id)["dish_count"],
                }
                for restaurant in restaurants
            ]
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
