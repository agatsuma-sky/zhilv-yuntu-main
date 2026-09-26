from __future__ import annotations

from datetime import date as DateType, timedelta
from math import radians, sin, cos, sqrt, atan2

from app.agents.trip_planner_agent import (
    DynamicPlannerDayDraft,
    DynamicPlannerDraft,
    collect_trip_context,
    generate_day_edit_draft,
    generate_dynamic_planner_draft,
    generate_planner_draft,
)
from app.config import ENABLE_AMAP_ENRICHMENT
from app.models.schemas import (
    BudgetBreakdown,
    DayPlan,
    HotelItem,
    Itinerary,
    MealItem,
    SpotItem,
    TokenUsage,
    TransportItem,
    TripEditRequest,
    TripRequest,
)
from app.services.map_service import enrich_itinerary_with_map_data
from app.services.fallback_candidates import extract_fallback_candidates
from app.services.place_candidate_service import (
    CityCandidatePool,
    PlaceCandidate,
    PlaceCandidateCategory,
)


def _haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """计算两个经纬度点之间的球面距离（公里）。"""
    # 地球半径（公里）
    R = 6371.0

    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return R * c


def _select_best_hotel_for_spots(
    hotels: list[PlaceCandidate],
    spots: list[PlaceCandidate],
    daily_budget: float | None = None,
    hotel_level: str | None = None,
) -> PlaceCandidate | None:
    """根据景点位置选择距离最近且符合预算的酒店。"""
    if not hotels or not spots:
        return hotels[0] if hotels else None

    # 计算所有景点的中心点
    avg_lat = sum(s.latitude for s in spots) / len(spots)
    avg_lon = sum(s.longitude for s in spots) / len(spots)

    # 如果有预算限制，先筛选价格合适的酒店
    candidate_hotels = hotels
    if daily_budget is not None and daily_budget > 0:
        min_price = daily_budget * 0.5
        max_price = daily_budget * 1.5
        budget_filtered = []
        for h in hotels:
            est_cost = _estimate_hotel_cost(h.name, hotel_level)
            if min_price <= est_cost <= max_price:
                budget_filtered.append(h)
        # 如果有符合预算的酒店，只在这些里面选；否则回退到所有酒店
        if budget_filtered:
            candidate_hotels = budget_filtered

    # 找到距离中心点最近的酒店
    best_hotel = min(
        candidate_hotels,
        key=lambda h: _haversine_distance(avg_lat, avg_lon, h.latitude, h.longitude),
    )
    return best_hotel


TECHNICAL_TIP_KEYWORDS = (
    "LLM",
    "RAG",
    "LangChain",
    "Chroma",
    "演示",
    "测试",
    "规则",
    "模型",
    "源码",
    "trip_service",
)


def _clean_user_tips(tips: list[str], destination: str | None = None) -> list[str]:
    """过滤内部实现说明，只保留用户真正能用到的旅行建议。"""
    cleaned_tips: list[str] = []
    for tip in tips:
        normalized_tip = tip.strip()
        if not normalized_tip:
            continue
        if any(keyword in normalized_tip for keyword in TECHNICAL_TIP_KEYWORDS):
            continue
        if normalized_tip not in cleaned_tips:
            cleaned_tips.append(normalized_tip)

    if cleaned_tips:
        return cleaned_tips

    place_text = destination or "目的地"
    return [
        f"建议根据{place_text}当天实时天气准备雨具或薄外套，早晚和临水区域体感可能偏凉。",
        "古镇、生态廊道和石板路更适合慢慢走，鞋子尽量选择舒适防滑的款式。",
        "热门景点建议错峰出发，给拍照、用餐和交通预留更从容的缓冲时间。",
    ]


def _requests_no_fixed_spot(instruction: str) -> bool:
    """识别明确取消景点的指令，避免把“不要安排太满”误判为空行程。"""
    normalized = "".join(instruction.split())
    return any(
        phrase in normalized
        for phrase in (
            "不要安排景点",
            "不安排景点",
            "取消景点",
            "不去景点",
            "改成自由活动",
        )
    )


def _stable_bucket(text: str, modulo: int) -> int:
    """基于文本生成一个稳定桶值，用来做确定性的价格浮动。"""
    return sum(ord(char) for char in text) % modulo if modulo > 0 else 0


def _prorate_amounts(total: float, weights: list[float]) -> list[float]:
    """按权重拆分金额，同时保证拆分后的总和与原总额一致。"""
    if not weights:
        return []

    safe_weights = [max(weight, 0.01) for weight in weights]
    total_cents = max(int(round(total * 100)), 0)
    weight_sum = sum(safe_weights)
    raw_cents = [(total_cents * weight) / weight_sum for weight in safe_weights]
    base_cents = [int(value) for value in raw_cents]
    remainder = total_cents - sum(base_cents)

    ranked_indexes = sorted(
        range(len(raw_cents)),
        key=lambda index: (raw_cents[index] - base_cents[index], -index),
        reverse=True,
    )
    for index in ranked_indexes[:remainder]:
        base_cents[index] += 1

    return [round(value / 100, 2) for value in base_cents]


def _estimate_ticket_cost(spot_name: str, description: str | None = None) -> float:
    """根据景点关键词估算门票，更接近真实行程而不是固定数值。"""
    text = f"{spot_name} {description or ''}"
    bucket = _stable_bucket(text, 4)

    if any(keyword in text for keyword in ("古城", "古镇", "公园", "廊道", "村", "湿地", "街区")):
        return [0.0, 20.0, 30.0, 40.0][bucket]
    if any(keyword in text for keyword in ("寺", "三塔", "博物馆", "遗址", "山庄")):
        return round(60.0 + (bucket * 18.0), 2)
    if any(keyword in text for keyword in ("索道", "缆车", "游船", "演出", "雪山")):
        return round(120.0 + (bucket * 28.0), 2)
    return round(35.0 + (bucket * 12.0), 2)


def _estimate_hotel_cost(hotel_name: str, hotel_level: str | None = None) -> float:
    """根据酒店名称和档次估算每晚价格（元）。"""
    text = f"{hotel_name} {hotel_level or ''}"
    bucket = _stable_bucket(text, 4)
    level = hotel_level or ""

    if "豪华" in level or "五星" in level or "五钻" in level or "奢华" in level:
        base, spread = 800.0, 400.0
    elif "高档" in level or "四星" in level or "高端" in level:
        base, spread = 500.0, 250.0
    elif "舒适" in level or "三星" in level:
        base, spread = 300.0, 150.0
    elif "经济" in level or "二星" in level or "青年旅社" in level or "民宿" in level:
        base, spread = 150.0, 80.0
    else:
        base, spread = 350.0, 200.0

    # 名称中的关键词微调
    if any(kw in text for kw in ("国际", "洲际", "希尔顿", "万豪", "香格里拉", "半岛", "丽思", "四季")):
        base *= 1.4
    elif any(kw in text for kw in ("如家", "汉庭", "7天", "锦江之星", "莫泰", "格林", "易佰")):
        base *= 0.7
    elif any(kw in text for kw in ("度假", "海景", "山景", "温泉", "SPA")):
        base *= 1.2

    return round(base + (bucket * spread / 4), 2)


def _build_hotel_weights(day_count: int, start_date: DateType) -> list[float]:
    """让住宿费用按周末、尾日等因素轻微浮动。"""
    weights: list[float] = []
    for index in range(day_count):
        current_date = start_date + timedelta(days=index)
        weight = 1.0
        if current_date.weekday() in (4, 5):
            weight += 0.18
        if index == day_count - 1:
            weight += 0.08
        if index % 2 == 1:
            weight += 0.05
        weights.append(weight)
    return weights


def _build_meal_weights(day_count: int, preferences: list[str]) -> list[float]:
    """让美食偏好的用户在部分天数获得更高餐饮预算。"""
    foodie_bonus = 0.12 if "美食" in preferences else 0.0
    return [
        1.0 + foodie_bonus + (0.08 if index == day_count // 2 else 0.0) + ((index % 3) * 0.04)
        for index in range(day_count)
    ]


def _build_transport_weights(day_count: int, pace: str | None) -> list[float]:
    """让交通预算随行程节奏和首尾日轻微浮动。"""
    pace_bonus = 0.12 if pace == "紧凑" else -0.04 if pace == "轻松" else 0.04
    return [
        1.0 + pace_bonus + (0.16 if index in (0, day_count - 1) else 0.0) + (index * 0.03)
        for index in range(day_count)
    ]


def _apply_route_based_transport_costs(itinerary: Itinerary) -> None:
    """在已有路线距离时，用路线信息修正交通花费和耗时。"""
    for day in itinerary.days:
        for transport in day.transport:
            if transport.estimated_minutes is not None:
                transport.duration = f"{transport.estimated_minutes} 分钟"

            if transport.distance_km is None:
                continue

            mode = transport.mode or ""
            if "公交" in mode:
                cost = max(2.0, 2.0 + (transport.distance_km * 0.25))
            elif "步行" in mode:
                cost = 0.0
            elif "包车" in mode:
                cost = 30.0 + (transport.distance_km * 3.8)
            else:
                cost = 10.0 + (transport.distance_km * 2.2)

            transport.estimated_cost = round(cost, 2)


def _refresh_budget_breakdown(itinerary: Itinerary, request_budget: float | None = None) -> Itinerary:
    """从具体条目回算预算汇总，避免预算明细显得过于模板化。"""
    _apply_route_based_transport_costs(itinerary)

    transport_total = round(
        sum(item.estimated_cost for day in itinerary.days for item in day.transport),
        2,
    )
    hotel_total = round(
        sum(day.hotel.estimated_cost for day in itinerary.days if day.hotel is not None),
        2,
    )
    meal_total = round(
        sum(item.estimated_cost for day in itinerary.days for item in day.meals),
        2,
    )
    ticket_total = round(
        sum(item.estimated_cost for day in itinerary.days for item in day.spots),
        2,
    )

    subtotal = transport_total + hotel_total + meal_total + ticket_total
    if request_budget is not None:
        other_total = round(max(0.0, min(request_budget * 0.12, request_budget - subtotal)), 2)
    else:
        other_total = round(max(subtotal * 0.06, 0.0), 2)

    total = round(subtotal + other_total, 2)
    itinerary.budget_breakdown = BudgetBreakdown(
        transport=transport_total,
        hotel=hotel_total,
        meals=meal_total,
        tickets=ticket_total,
        other=other_total,
        total=total,
    )
    itinerary.estimated_budget = total
    return itinerary


def _maybe_enrich_itinerary_with_map_data(
    itinerary: Itinerary,
    city: str | None = None,
    request_budget: float | None = None,
) -> Itinerary:
    """按开关补充地图信息，并在最后统一刷新预算。"""
    if ENABLE_AMAP_ENRICHMENT:
        try:
            itinerary = enrich_itinerary_with_map_data(itinerary, city=city)
        except Exception:
            pass

    return _refresh_budget_breakdown(itinerary, request_budget=request_budget)


def _validated_dynamic_draft(
    draft: DynamicPlannerDraft | None,
    candidate_pool: CityCandidatePool,
    day_count: int,
) -> DynamicPlannerDraft | None:
    """拒绝包含候选池外 ID 或重复 day_index 的动态 Planner 结果。"""
    if draft is None:
        return None

    candidate_ids = {
        category: {
            candidate.poi_id
            for candidate in candidate_pool.candidates_for(category)
        }
        for category in PlaceCandidateCategory
    }
    if draft.hotel_poi_id not in candidate_ids[PlaceCandidateCategory.HOTEL]:
        return None
    if sorted(day.day_index for day in draft.days) != list(range(1, day_count + 1)):
        return None

    for day in draft.days:
        # 兼容新旧格式：spot_poi_ids (list) 或 spot_poi_id (str)
        spot_ids = []
        if hasattr(day, 'spot_poi_ids') and day.spot_poi_ids:
            spot_ids = day.spot_poi_ids
        elif hasattr(day, 'spot_poi_id') and day.spot_poi_id:
            spot_ids = [day.spot_poi_id]
        
        if not spot_ids:
            return None
        
        for spot_id in spot_ids:
            if spot_id not in candidate_ids[PlaceCandidateCategory.SPOT]:
                return None
        
        if day.meal_poi_id not in candidate_ids[PlaceCandidateCategory.MEAL]:
            return None

    return draft


def _candidate_map(
    candidate_pool: CityCandidatePool,
    category: PlaceCandidateCategory,
) -> dict[str, PlaceCandidate]:
    return {
        candidate.poi_id: candidate
        for candidate in candidate_pool.candidates_for(category)
    }


def generate_dynamic_trip_itinerary(
    request: TripRequest,
    candidate_pool: CityCandidatePool,
) -> Itinerary:
    """使用地图候选生成动态城市行程，所有展示实体都由 POI ID 回填。"""
    day_count = max((request.end_date - request.start_date).days + 1, 1)
    raw_draft, planner_usage = generate_dynamic_planner_draft(
        request=request,
        candidate_pool=candidate_pool,
        day_count=day_count,
    )
    draft = _validated_dynamic_draft(raw_draft, candidate_pool, day_count)

    spots = candidate_pool.candidates_for(PlaceCandidateCategory.SPOT)
    meals = candidate_pool.candidates_for(PlaceCandidateCategory.MEAL)
    hotels = candidate_pool.candidates_for(PlaceCandidateCategory.HOTEL)
    if not spots or not meals or not hotels:
        raise ValueError("动态城市候选池缺少景点、餐饮或住宿，无法生成行程。")

    spot_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.SPOT)
    meal_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.MEAL)
    hotel_by_id = _candidate_map(candidate_pool, PlaceCandidateCategory.HOTEL)

    # 先计算 ticket_costs（在选酒店前需要）
    ticket_costs: list[float] = []
    for index in range(day_count):
        day_number = index + 1
        planner_day = (
            next((day for day in draft.days if day.day_index == day_number), None)
            if draft is not None
            else None
        )
        
        # 处理多景点 - 使用模型的统一获取方法
        if planner_day is not None:
            spot_poi_ids = planner_day.get_spot_poi_ids()
        else:
            spot_poi_ids = [spots[index % len(spots)].poi_id]
        
        selected_spots = [spot_by_id[pid] for pid in spot_poi_ids if pid in spot_by_id]
        if not selected_spots:
            selected_spots = [spots[index % len(spots)]]
        
        # 计算当天总门票费用（所有景点之和）
        day_ticket_cost = sum(
            _estimate_ticket_cost(s.name, s.type_name) for s in selected_spots
        )
        ticket_costs.append(day_ticket_cost)

    ticket_total = round(sum(ticket_costs), 2)
    target_total = request.budget * (
        0.78 if request.pace == "轻松" else 0.92 if request.pace == "紧凑" else 0.85
    )
    other_budget = round(request.budget * (0.05 + min(day_count, 4) * 0.01), 2)
    allocatable_budget = max(
        target_total - ticket_total - other_budget,
        request.budget * 0.45,
    )

    hotel_level = request.hotel_level or "舒适型"
    if "豪华" in hotel_level:
        hotel_ratio = 0.62
    elif "高档" in hotel_level or "高端" in hotel_level:
        hotel_ratio = 0.56
    elif "经济" in hotel_level:
        hotel_ratio = 0.40
    else:
        hotel_ratio = 0.50
    meal_ratio = 0.28 if "美食" in request.preferences else 0.22
    transport_ratio = max(0.12, 1 - hotel_ratio - meal_ratio)
    ratio_sum = hotel_ratio + meal_ratio + transport_ratio

    daily_hotel_costs = _prorate_amounts(
        allocatable_budget * hotel_ratio / ratio_sum,
        _build_hotel_weights(day_count, request.start_date),
    )

    selected_days: list[
        tuple[list[PlaceCandidate], PlaceCandidate, PlaceCandidate | None, DynamicPlannerDayDraft | None]
    ] = []
    for index in range(day_count):
        day_number = index + 1
        planner_day = (
            next((day for day in draft.days if day.day_index == day_number), None)
            if draft is not None
            else None
        )
        
        # 处理多景点 - 使用模型的统一获取方法
        if planner_day is not None:
            spot_poi_ids = planner_day.get_spot_poi_ids()
        else:
            spot_poi_ids = [spots[index % len(spots)].poi_id]
        
        selected_spots = [spot_by_id[pid] for pid in spot_poi_ids if pid in spot_by_id]
        if not selected_spots:
            selected_spots = [spots[index % len(spots)]]
        
        selected_meal = (
            meal_by_id[planner_day.meal_poi_id]
            if planner_day is not None
            else meals[index % len(meals)]
        )
        
        # 按天选择最近且符合预算的酒店
        daily_budget = daily_hotel_costs[index]
        
        # 如果 LLM 指定了 hotel_poi_id 且在候选池中，作为首选备选
        llm_hotel = None
        if draft is not None and draft.hotel_poi_id in hotel_by_id:
            llm_hotel = hotel_by_id[draft.hotel_poi_id]
        
        # 选择距离当天景点最近且符合预算的酒店
        if llm_hotel is not None:
            # 如果 LLM 指定的酒店在合理距离内（50km）且价格合适，优先使用
            llm_hotel_cost = _estimate_hotel_cost(llm_hotel.name, hotel_level)
            min_price = daily_budget * 0.5
            max_price = daily_budget * 1.5
            avg_lat = sum(s.latitude for s in selected_spots) / len(selected_spots)
            avg_lon = sum(s.longitude for s in selected_spots) / len(selected_spots)
            dist = _haversine_distance(avg_lat, avg_lon, llm_hotel.latitude, llm_hotel.longitude)
            if dist <= 50.0 and min_price <= llm_hotel_cost <= max_price:
                selected_hotel = llm_hotel
            else:
                selected_hotel = _select_best_hotel_for_spots(hotels, selected_spots, daily_budget, hotel_level)
        else:
            selected_hotel = _select_best_hotel_for_spots(hotels, selected_spots, daily_budget, hotel_level)
        
        selected_days.append((selected_spots, selected_meal, selected_hotel, planner_day))
        
        # 计算当天总门票费用（所有景点之和）
        day_ticket_cost = sum(
            _estimate_ticket_cost(s.name, s.type_name) for s in selected_spots
        )
        ticket_costs.append(day_ticket_cost)
    daily_meal_costs = _prorate_amounts(
        allocatable_budget * meal_ratio / ratio_sum,
        _build_meal_weights(day_count, request.preferences),
    )
    daily_transport_costs = _prorate_amounts(
        allocatable_budget * transport_ratio / ratio_sum,
        _build_transport_weights(day_count, request.pace),
    )

    days: list[DayPlan] = []
    for index, (selected_spots, meal, selected_hotel, planner_day) in enumerate(selected_days):
        # 使用模型的统一获取方法，自动兼容新旧格式
        spot_reasons = planner_day.get_spot_reasons() if planner_day is not None else ["该地点来自当前城市的高德 POI 候选池。"] * len(selected_spots)
        spot_start_times = planner_day.get_spot_start_times() if planner_day is not None else ["10:00"] * len(selected_spots)
        spot_end_times = planner_day.get_spot_end_times() if planner_day is not None else ["12:00"] * len(selected_spots)
        
        meal_notes = (
            planner_day.meal_notes
            if planner_day is not None
            else "餐厅来自当前城市的高德 POI 候选池，请以实际营业信息为准。"
        )
        daily_note = (
            planner_day.daily_note
            if planner_day is not None
            else "按真实地点候选安排，出发前请再次确认开放时间和交通情况。"
        )
        theme = (
            planner_day.theme
            if planner_day is not None
            else f"{request.destination}第 {index + 1} 天探索"
        )
        current_date = request.start_date + timedelta(days=index)
        
        # 为每个景点创建 SpotItem
        spot_items = []
        for i, spot in enumerate(selected_spots):
            spot_items.append(
                SpotItem(
                    name=spot.name,
                    start_time=spot_start_times[i] if i < len(spot_start_times) else "10:00",
                    end_time=spot_end_times[i] if i < len(spot_end_times) else "12:00",
                    description=spot_reasons[i] if i < len(spot_reasons) else "该地点来自当前城市的高德 POI 候选池。",
                    estimated_cost=ticket_costs[index] / len(selected_spots) if selected_spots else 0.0,
                    location=spot.district or spot.city or request.destination,
                    image_url=spot.image_url,
                    address=spot.address,
                    latitude=spot.latitude,
                    longitude=spot.longitude,
                    poi_id=spot.poi_id,
                )
            )
        
        days.append(
            DayPlan(
                day_index=index + 1,
                date=current_date,
                theme=theme,
                spots=spot_items,
                meals=[
                    MealItem(
                        name=meal.name,
                        meal_type="午餐",
                        estimated_cost=daily_meal_costs[index],
                        notes=meal_notes,
                        address=meal.address,
                        latitude=meal.latitude,
                        longitude=meal.longitude,
                        poi_id=meal.poi_id,
                        image_url=meal.image_url,
                    )
                ],
                hotel=HotelItem(
                    name=selected_hotel.name,
                    level=hotel_level,
                    estimated_cost=daily_hotel_costs[index],
                    location=(
                        selected_hotel.district
                        or selected_hotel.city
                        or request.destination
                    ),
                    address=selected_hotel.address,
                    latitude=selected_hotel.latitude,
                    longitude=selected_hotel.longitude,
                    poi_id=selected_hotel.poi_id,
                    image_url=selected_hotel.image_url,
                ),
                transport=[
                    TransportItem(
                        mode="公共交通 / 打车",
                        from_place=selected_hotel.name,
                        to_place=spot_items[0].name if spot_items else selected_hotel.name,
                        estimated_cost=daily_transport_costs[index],
                        duration="请以实时地图路线为准",
                    )
                ],
                notes=[
                    f"当前旅行节奏：{request.pace or '适中'}",
                    daily_note,
                    "门票、餐饮、住宿和交通金额均为预算估算，不代表实时可订价格。",
                ],
            )
        )

    preference_text = "、".join(request.preferences) if request.preferences else "常规旅行体验"
    summary = (
        draft.summary
        if draft is not None
        else f"这是一份为{request.destination}生成的 {day_count} 日动态行程，偏好重点为：{preference_text}。"
    )
    tips = _clean_user_tips(
        draft.tips if draft is not None else [],
        request.destination,
    )
    source_notes = [
        "本行程的景点、餐饮和住宿实体均由本次高德 POI 候选池回填。",
        "预算为规划估算，地点开放、营业和可订状态请以出发前实时信息为准。",
    ]
    if raw_draft is not None and draft is None:
        source_notes.append("动态 Planner 返回了候选池外数据，已自动改用真实候选规则方案。")
    elif draft is None:
        source_notes.append("动态 Planner 当前不可用，已使用真实 POI 候选生成规则方案。")

    itinerary = Itinerary(
        trip_id=f"trip_{request.destination}_{request.start_date.isoformat()}",
        destination=request.destination,
        summary=summary,
        days=days,
        estimated_budget=0.0,
        budget_breakdown=BudgetBreakdown(),
        tips=tips,
        source_notes=source_notes,
        token_usage=TokenUsage(
            planner_prompt_tokens=planner_usage.get("prompt_tokens", 0),
            planner_completion_tokens=planner_usage.get("completion_tokens", 0),
        ),
    )
    return _refresh_budget_breakdown(itinerary, request_budget=request.budget)


def generate_trip_itinerary(request: TripRequest) -> Itinerary:
    """生成完整 itinerary，并使用更真实的预算估算方式。"""
    day_count = (request.end_date - request.start_date).days + 1
    day_count = max(day_count, 1)

    rag_contexts, rewrite_usage, rerank_usage, embedding_usage = collect_trip_context(
        destination=request.destination,
        preferences=request.preferences,
        pace=request.pace,
        special_notes=request.special_notes,
    )
    llm_draft, planner_usage = generate_planner_draft(request, rag_contexts, day_count)

    token_usage = TokenUsage(
        rewrite_prompt_tokens=rewrite_usage.get("prompt_tokens", 0),
        rewrite_completion_tokens=rewrite_usage.get("completion_tokens", 0),
        embedding_prompt_tokens=embedding_usage.get("prompt_tokens", 0),
        embedding_completion_tokens=embedding_usage.get("completion_tokens", 0),
        planner_prompt_tokens=planner_usage.get("prompt_tokens", 0),
        planner_completion_tokens=planner_usage.get("completion_tokens", 0),
        rerank_prompt_tokens=rerank_usage.get("prompt_tokens", 0),
        rerank_completion_tokens=rerank_usage.get("completion_tokens", 0),
    )
    print(
        "[token_usage] Query Rewrite: "
        f"prompt={token_usage.rewrite_prompt_tokens}, "
        f"completion={token_usage.rewrite_completion_tokens}"
    )
    print(
        "[token_usage] Rerank: "
        f"prompt={token_usage.rerank_prompt_tokens}, "
        f"completion={token_usage.rerank_completion_tokens}"
    )
    print(
        "[token_usage] Query Embedding: "
        f"prompt={token_usage.embedding_prompt_tokens}, "
        f"completion={token_usage.embedding_completion_tokens}"
    )
    print(
        "[token_usage] Planner: "
        f"prompt={token_usage.planner_prompt_tokens}, "
        f"completion={token_usage.planner_completion_tokens}"
    )
    print(
        "[token_usage] Total: "
        f"prompt={token_usage.total_prompt_tokens}, "
        f"completion={token_usage.total_completion_tokens}, "
        f"all={token_usage.total_tokens}"
    )
    fallback_candidates = extract_fallback_candidates(rag_contexts)
    fallback_spot_names = fallback_candidates["spots"]
    fallback_meal_names = fallback_candidates["meals"]
    fallback_hotel_names = fallback_candidates["hotels"]
    
    # 预估门票总费用来分配预算（在主循环后会用更精确的 per-day 计算，但此处需要提前估算）
    _pre_ticket_costs = []
    for _idx in range(day_count):
        _spot_name = fallback_spot_names[_idx] if _idx < len(fallback_spot_names) else ""
        _spot_desc = ""
        if llm_draft is not None:
            _llm_day = next((d for d in llm_draft.days if d.day_index == _idx + 1), None)
            if _llm_day is not None:
                _spot_name = getattr(_llm_day, "spot_name", "") or _spot_name
                _spot_desc = getattr(_llm_day, "spot_description", "") or ""
        _pre_ticket_costs.append(_estimate_ticket_cost(_spot_name, _spot_desc) if _spot_name else 0.0)
    
    _ticket_total = round(sum(_pre_ticket_costs), 2)
    _target_total = request.budget * (
        0.78 if request.pace == "轻松" else 0.92 if request.pace == "紧凑" else 0.85
    )
    _other_budget = round(request.budget * (0.05 + min(day_count, 4) * 0.01), 2)
    allocatable_budget = max(
        _target_total - _ticket_total - _other_budget,
        request.budget * 0.45,
    )

    # 先计算每日酒店预算
    hotel_level = request.hotel_level or "舒适型"
    if "豪华" in hotel_level:
        hotel_ratio = 0.62
    elif "高档" in hotel_level or "高端" in hotel_level:
        hotel_ratio = 0.56
    elif "经济" in hotel_level:
        hotel_ratio = 0.40
    else:
        hotel_ratio = 0.50

    meal_ratio = 0.28 if "美食" in request.preferences else 0.22
    transport_ratio = max(0.12, 1 - hotel_ratio - meal_ratio)
    ratio_sum = hotel_ratio + meal_ratio + transport_ratio

    hotel_total = allocatable_budget * hotel_ratio / ratio_sum
    meal_total = allocatable_budget * meal_ratio / ratio_sum
    transport_total = allocatable_budget * transport_ratio / ratio_sum

    daily_hotel_costs = _prorate_amounts(
        hotel_total,
        _build_hotel_weights(day_count, request.start_date),
    )
    daily_meal_costs = _prorate_amounts(
        meal_total,
        _build_meal_weights(day_count, request.preferences),
    )
    daily_transport_costs = _prorate_amounts(
        transport_total,
        _build_transport_weights(day_count, request.pace),
    )

    # 为每一天选择符合预算的酒店
    selected_hotels_per_day: list[str | None] = []
    for index in range(day_count):
        daily_budget = daily_hotel_costs[index]
        # 估算每个候选酒店的价格，选择最接近预算的
        if fallback_hotel_names:
            best_hotel = min(
                fallback_hotel_names,
                key=lambda name: abs(_estimate_hotel_cost(name, hotel_level) - daily_budget)
            )
            selected_hotels_per_day.append(best_hotel)
        else:
            selected_hotels_per_day.append(None)

    raw_days: list[dict[str, object]] = []
    ticket_costs: list[float] = []
    for index in range(day_count):
        day_number = index + 1
        current_date = request.start_date + timedelta(days=index)
        llm_day = None
        if llm_draft is not None:
            llm_day = next((item for item in llm_draft.days if item.day_index == day_number), None)

        if llm_day is not None:
            # 多景点模式
            spot_names = llm_day.spot_names if hasattr(llm_day, 'spot_names') and llm_day.spot_names else (
                [llm_day.spot_name] if hasattr(llm_day, 'spot_name') and llm_day.spot_name else []
            )
            spot_descriptions = llm_day.spot_descriptions if hasattr(llm_day, 'spot_descriptions') and llm_day.spot_descriptions else (
                [llm_day.spot_description] if hasattr(llm_day, 'spot_description') and llm_day.spot_description else []
            )
        else:
            # 回退模式
            spot_names = [fallback_spot_names[index]] if index < len(fallback_spot_names) else []
            spot_descriptions = ["根据本地攻略检索到的景点信息安排。"] * len(spot_names) if spot_names else []

        theme = llm_day.theme if llm_day is not None else f"{request.destination} 第 {day_number} 天轻松游"
        meal_name = (
            llm_day.meal_name
            if llm_day is not None
            else fallback_meal_names[index] if index < len(fallback_meal_names) else None
        )
        meal_note = (
            llm_day.meal_notes
            if llm_day is not None
            else "来自本地攻略的餐饮条目。" if meal_name else None
        )
        daily_note = (
            llm_day.daily_note
            if llm_day is not None
            else "今天以轻松游览为主，建议根据体力和天气灵活调整停留时间。"
        )
        unavailable_notes: list[str] = []
        if not spot_names:
            unavailable_notes.append("未从当前攻略检索到景点信息，今天未安排景点。")
        if not meal_name:
            unavailable_notes.append("未从当前攻略检索到餐饮信息，今天未安排餐饮。")
        _selected_hotel = selected_hotels_per_day[index] if index < len(selected_hotels_per_day) else None
        if _selected_hotel is None:
            unavailable_notes.append("未从当前攻略检索到住宿信息，未安排住宿。")

        # 计算当天总门票费用（所有景点之和）
        day_ticket_cost = sum(
            _estimate_ticket_cost(name, desc) for name, desc in zip(spot_names, spot_descriptions)
        ) if spot_names else 0.0

        raw_days.append(
            {
                "day_index": day_number,
                "date": current_date,
                "theme": theme,
                "spot_names": spot_names,
                "spot_descriptions": spot_descriptions,
                "meal_name": meal_name,
                "meal_note": meal_note,
                "daily_note": daily_note,
                "unavailable_notes": unavailable_notes,
                "ticket_cost": day_ticket_cost,
            }
        )
        ticket_costs.append(day_ticket_cost)

    ticket_total = round(sum(ticket_costs), 2)
    target_total = request.budget * (
        0.78 if request.pace == "轻松" else 0.92 if request.pace == "紧凑" else 0.85
    )
    other_budget = round(request.budget * (0.05 + min(day_count, 4) * 0.01), 2)
    allocatable_budget = max(target_total - ticket_total - other_budget, request.budget * 0.45)

    hotel_level = request.hotel_level or "舒适型"
    if "豪华" in hotel_level:
        hotel_ratio = 0.62
    elif "高档" in hotel_level or "高端" in hotel_level:
        hotel_ratio = 0.56
    elif "经济" in hotel_level:
        hotel_ratio = 0.40
    else:
        hotel_ratio = 0.50

    meal_ratio = 0.28 if "美食" in request.preferences else 0.22
    transport_ratio = max(0.12, 1 - hotel_ratio - meal_ratio)
    ratio_sum = hotel_ratio + meal_ratio + transport_ratio

    hotel_total = allocatable_budget * hotel_ratio / ratio_sum
    meal_total = allocatable_budget * meal_ratio / ratio_sum
    transport_total = allocatable_budget * transport_ratio / ratio_sum

    daily_hotel_costs = _prorate_amounts(
        hotel_total,
        _build_hotel_weights(day_count, request.start_date),
    )
    daily_meal_costs = _prorate_amounts(
        meal_total,
        _build_meal_weights(day_count, request.preferences),
    )
    daily_transport_costs = _prorate_amounts(
        transport_total,
        _build_transport_weights(day_count, request.pace),
    )

    days: list[DayPlan] = []
    for index, raw_day in enumerate(raw_days):
        spot_names = raw_day.get("spot_names", [])
        if not spot_names and raw_day.get("spot_name"):
            # 兼容旧格式
            spot_names = [raw_day["spot_name"]]
        spot_descriptions = raw_day.get("spot_descriptions", [])
        if not spot_descriptions and raw_day.get("spot_description"):
            spot_descriptions = [raw_day["spot_description"]]
        meal_name = raw_day["meal_name"]
        daily_notes = [
            f"当前旅行节奏：{request.pace or '适中'}",
            str(raw_day["daily_note"]),
            *[str(note) for note in raw_day["unavailable_notes"]],
        ]
        # 为每个景点分配时间和费用
        spot_items = []
        if spot_names:
            num_spots = len(spot_names)
            # 简单平均分配门票费用
            per_spot_cost = float(raw_day["ticket_cost"]) / num_spots if raw_day["ticket_cost"] else 0.0
            # 简单平均分配时间（从 10:00 开始，每个景点 2 小时）
            for i, name in enumerate(spot_names):
                start_hour = 10 + i * 2
                end_hour = start_hour + 2
                desc = spot_descriptions[i] if i < len(spot_descriptions) else "根据本地攻略安排。"
                spot_items.append(
                    SpotItem(
                        name=str(name),
                        start_time=f"{start_hour:02d}:00",
                        end_time=f"{end_hour:02d}:00",
                        description=str(desc),
                        estimated_cost=per_spot_cost,
                        location=request.destination,
                    )
                )

        day_plan = DayPlan(
            day_index=int(raw_day["day_index"]),
            date=raw_day["date"],
            theme=str(raw_day["theme"]),
            spots=spot_items,
            meals=(
                [
                    MealItem(
                        name=str(meal_name),
                        meal_type="午餐",
                        estimated_cost=daily_meal_costs[index],
                        notes=str(raw_day["meal_note"]),
                    )
                ]
                if meal_name
                else []
            ),
            hotel=(
                HotelItem(
                    name=selected_hotels_per_day[index],
                    level=hotel_level,
                    estimated_cost=daily_hotel_costs[index],
                    location=request.destination,
                )
                if selected_hotels_per_day[index]
                else None
            ),
            transport=(
                [
                    TransportItem(
                        mode="打车",
                        from_place=f"{request.destination} 出发点",
                        to_place=str(spot_names[0]) if spot_names else request.destination,
                        estimated_cost=daily_transport_costs[index],
                        duration="30 分钟",
                    )
                ]
                if spot_names
                else []
            ),
            notes=daily_notes,
        )
        days.append(day_plan)

    preference_text = "、".join(request.preferences) if request.preferences else "常规旅行体验"
    source_notes = [
        "Itinerary is assembled by trip_service.py and can optionally use LangChain structured output.",
    ]
    source_notes.extend(rag_contexts[:2])

    tips = (
        llm_draft.tips
        if llm_draft is not None and llm_draft.tips
        else [
            f"建议根据{request.destination}当天实时天气准备雨具或薄外套。",
            "古镇、生态廊道和石板路更适合慢慢走，鞋子尽量选择舒适防滑的款式。",
        ]
    )
    if any("骑行" in context for context in rag_contexts):
        tips.append("如计划骑行，请以当地实时路况和可通行区域为准。")
    tips = _clean_user_tips(tips, request.destination)

    summary = (
        llm_draft.summary
        if llm_draft is not None
        else (
            f"这是一份为 {request.destination} 生成的 {day_count} 日行程，偏好重点为：{preference_text}。"
            "未检索到的信息不会以虚构地点补充。"
        )
    )

    itinerary = Itinerary(
        trip_id=f"trip_{request.destination}_{request.start_date.isoformat()}",
        destination=request.destination,
        summary=summary,
        days=days,
        estimated_budget=0.0,
        budget_breakdown=BudgetBreakdown(),
        tips=tips,
        source_notes=source_notes,
        token_usage=token_usage,
    )
    return _maybe_enrich_itinerary_with_map_data(
        itinerary,
        city=request.destination,
        request_budget=request.budget,
    )


def edit_trip_itinerary(request: TripEditRequest) -> Itinerary:
    """优先使用 LLM 编辑单日行程，失败时回退到规则编辑。"""
    updated_itinerary = request.current_itinerary.model_copy(deep=True)

    target_day = updated_itinerary.days[0] if updated_itinerary.days else None
    if request.edit_scope and request.edit_scope.startswith("day_"):
        try:
            target_day_index = int(request.edit_scope.split("_")[1])
            matched_day = next(
                (day for day in updated_itinerary.days if day.day_index == target_day_index),
                None,
            )
            if matched_day is not None:
                target_day = matched_day
        except (IndexError, ValueError):
            pass

    llm_edit_applied = False
    edit_token_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if target_day is not None:
        day_uses_candidate_pool = bool(
            target_day.hotel is not None
            and target_day.hotel.poi_id
            and target_day.spots
            and target_day.spots[0].poi_id
            and target_day.meals
            and target_day.meals[0].poi_id
        )
        day_edit_draft, edit_token_usage = generate_day_edit_draft(request, target_day)
        if day_edit_draft is not None:
            target_day.theme = day_edit_draft.theme
            if target_day.spots:
                spot_is_grounded = (
                    day_uses_candidate_pool
                    and target_day.spots[0].poi_id is not None
                )
                if not spot_is_grounded:
                    target_day.spots[0].name = day_edit_draft.spot_name
                target_day.spots[0].description = day_edit_draft.spot_description
                target_day.spots[0].estimated_cost = _estimate_ticket_cost(
                    target_day.spots[0].name,
                    day_edit_draft.spot_description,
                )
                if not spot_is_grounded:
                    target_day.spots[0].address = None
                    target_day.spots[0].latitude = None
                    target_day.spots[0].longitude = None
                    target_day.spots[0].poi_id = None
            if target_day.meals:
                if not day_uses_candidate_pool:
                    target_day.meals[0].name = day_edit_draft.meal_name
                target_day.meals[0].notes = day_edit_draft.meal_notes

            if target_day.notes:
                target_day.notes[-1] = day_edit_draft.daily_note
            else:
                target_day.notes.append(day_edit_draft.daily_note)

            llm_edit_applied = True
        else:
            if "轻松" in request.user_instruction:
                target_day.theme = f"{target_day.theme}（已调整为更轻松）"
                target_day.notes.append("已根据用户要求把节奏调整得更轻松。")

        if _requests_no_fixed_spot(request.user_instruction):
            removed_spot_names = {spot.name for spot in target_day.spots}
            target_day.spots = []
            target_day.transport = [
                transport
                for transport in target_day.transport
                if transport.from_place not in removed_spot_names
                and transport.to_place not in removed_spot_names
            ]
            target_day.notes.append(
                "已根据你的要求取消固定景点，保留自由活动时间。"
            )

    updated_itinerary.source_notes.append(
        f"已根据用户编辑指令更新行程：{request.user_instruction}"
    )
    updated_itinerary.tips = _clean_user_tips(
        updated_itinerary.tips,
        updated_itinerary.destination,
    )
    updated_itinerary.tips.append("已根据你的修改要求更新目标日期，出发前建议再确认当天交通、天气和景点开放情况。")

    updated_itinerary.token_usage = TokenUsage(
        rewrite_prompt_tokens=0,
        rewrite_completion_tokens=0,
        planner_prompt_tokens=edit_token_usage.get("prompt_tokens", 0),
        planner_completion_tokens=edit_token_usage.get("completion_tokens", 0),
    )

    reference_budget = (
        updated_itinerary.estimated_budget
        or updated_itinerary.budget_breakdown.total
        or None
    )
    return _maybe_enrich_itinerary_with_map_data(
        updated_itinerary,
        city=updated_itinerary.destination,
        request_budget=reference_budget,
    )
