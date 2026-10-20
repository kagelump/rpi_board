"""Scene lighting is tied to the forecast, independently of the run clock."""


def artwork_context(payload):
    day = payload.get("day_context", {})
    target = (day.get("target_date_iso") or day.get("date_iso")
              or payload.get("today", {}).get("daily_summary", {}).get("date", ""))
    period = "afternoon" if day.get("daypart_role") == "afternoon" else "daytime"
    return {
        "target_date": target,
        "scene_period": period,
        "guidance": (
            f"Show the forecast day's {period} outdoor conditions in daylight, "
            "regardless of the time this image is generated. Overcast or rainy "
            "daylight can be subdued; do not force sunshine. Keep night-only "
            "weather as advice in the copy rather than turning the picture into "
            "a night scene. Use a daylight sky wherever sky is visible, with no "
            "moon, stars, night sky or artificial night lighting. A black ink "
            "field is a print choice, not a change of time of day. This lighting "
            "direction overrides conflicting night cues in a legacy scene, "
            "style, palette or recipe; keep other daylight-compatible details."
        ),
    }
