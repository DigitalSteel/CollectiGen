"""Bounded final-size contract separate from native video sampling dimensions."""


def validate_video_output_goal(request):
    extras = request.extra_fields
    if not isinstance(extras, dict) or set(extras) - {"output_goal"}:
        raise ValueError("Unknown video request fields need an explicit adapter mapping.")
    goal = extras.get("output_goal")
    if goal is None:
        return None
    if (not isinstance(goal, dict) or set(goal) != {"final_width", "final_height", "detail_preference"}
            or goal["detail_preference"] != "faithful"):
        raise ValueError("Only a reviewed faithful final-video resize is supported.")
    width, height = goal["final_width"], goal["final_height"]
    if (type(width) is not int or type(height) is not int or width % 2 or height % 2
            or not 64 <= width <= 4096 or not 64 <= height <= 4096 or width * height > 8_294_400):
        raise ValueError("Choose an even final video canvas up to 4096 pixels per edge and 8.3 megapixels.")
    if (width, height) == (request.width, request.height):
        return None
    if (type(request.storage_budget_bytes) is not int or type(request.expected_output_bytes) is not int
            or request.storage_budget_bytes < 2 * request.expected_output_bytes):
        raise ValueError("Reserve space for both native and final video outputs before submission.")
    return dict(goal)
