from pathlib import Path
from unittest.mock import AsyncMock
import importlib.util
import json

import httpx
import pytest


spec = importlib.util.spec_from_file_location("steam_browser", Path(__file__).parents[1] / "plugin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def plugin():
    result = module.create_plugin()
    result.set_plugin_config(
        {
            "plugin": {"config_version": "0.1.0"},
            "api_key": "test-secret",
            "default_player": "76561198000000000",
        }
    )
    return result


@pytest.mark.parametrize("value", ["620", "https://store.steampowered.com/app/620/Portal_2/?l=english"])
def test_app_id(value):
    assert module.app_id(value) == 620


@pytest.mark.parametrize(
    "value", ["0", "-1", "4294967296", "https://evil.test/app/620", "https://store.steampowered.com.evil/app/620"]
)
def test_invalid_app_id(value):
    with pytest.raises(ValueError):
        module.app_id(value)


@pytest.mark.parametrize("method", ["game_details", "game_reviews"])
async def test_game_name_searches_then_fetches(plugin, monkeypatch, method):
    search = {
        "items": [
            {"id": 123, "name": "Battlefield 6 Deluxe", "type": "app"},
            {"id": 456, "name": "Battlefield™ 6", "type": "app"},
        ]
    }
    response = (
        {"456": {"success": True, "data": {"name": "Battlefield 6"}}}
        if method == "game_details"
        else {"response": {"reviews": []}}
    )
    request = AsyncMock(side_effect=[search, response])
    monkeypatch.setattr(plugin, "_get", request)
    result = await getattr(plugin, method)(app="Battlefield 6")
    assert result["success"] is True
    assert result["appid"] == 456
    first = request.call_args_list[0]
    assert first.args[1] == "/api/storesearch/"
    assert first.args[2]["term"] == "Battlefield 6"
    assert first.args[2]["l"] == "english"
    assert first.args[2]["cc"] == "cn"
    second = request.call_args_list[1]
    if method == "game_details":
        assert second.args[2]["appids"] == 456
    else:
        assert json.loads(second.args[2]["input_json"])["appid"] == 456


@pytest.mark.parametrize("method", ["game_details", "game_reviews"])
@pytest.mark.parametrize(
    "candidates,error",
    [
        ([], "game_not_found"),
        ([{"id": 1, "name": "Portal", "type": "app"}, {"id": 2, "name": "Portal 2", "type": "app"}], "ambiguous_game"),
    ],
)
async def test_name_resolution_stops_when_not_unique(plugin, monkeypatch, method, candidates, error):
    request = AsyncMock(return_value={"items": candidates})
    monkeypatch.setattr(plugin, "_get", request)
    result = await getattr(plugin, method)(app="传送门")
    assert result["success"] is False
    assert result["error"] == error
    assert request.await_count == 1
    if candidates:
        assert [item["id"] for item in result["candidates"]] == [1, 2]
        # 模拟宿主取正文的优先级，保证后续模型无需另取结构化结果就能继续查询。
        history_content = result.get("content", result.get("message", ""))
        for item in result["candidates"]:
            assert item["name"] in history_content
            assert f"appid={item['id']}" in history_content
            assert item["url"] in history_content


async def test_only_candidate_resolves_localized_name(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_get",
        AsyncMock(
            return_value={
                "items": [
                    {"id": 620, "name": "Portal 2", "type": "app"},
                ]
            }
        ),
    )
    assert await plugin._resolve_app("传送门2") == 620


@pytest.mark.parametrize("value", ["", "0", "-1", "4294967296", "https://evil.test/app/620"])
async def test_invalid_id_or_link_does_not_search(plugin, monkeypatch, value):
    request = AsyncMock()
    monkeypatch.setattr(plugin, "_get", request)
    assert (await plugin.game_details(value))["error"] == "invalid_app"
    request.assert_not_called()


async def test_name_search_network_failure_stops_lookup(plugin, monkeypatch):
    request = AsyncMock(side_effect=RuntimeError("Steam ConnectTimeout"))
    monkeypatch.setattr(plugin, "_get", request)
    with pytest.raises(RuntimeError, match="ConnectTimeout"):
        await plugin.game_details("Battlefield 6")
    assert request.await_count == 1


async def test_vanity_resolution_and_profile(plugin, monkeypatch):
    request = AsyncMock(
        side_effect=[
            {"response": {"success": 1, "steamid": "76561198000000000"}},
            {
                "response": {
                    "players": [
                        {"steamid": "76561198000000000", "personaname": "Example", "communityvisibilitystate": 1}
                    ]
                }
            },
        ]
    )
    monkeypatch.setattr(plugin, "_get", request)
    result = await plugin.player_profile("https://steamcommunity.com/id/example/")
    assert result["profile"]["personaname"] == "Example"
    assert "personastate" not in result["profile"]
    assert request.call_args_list[0].args[2]["vanityurl"] == "example"
    assert request.call_args_list[1].args[1] == "/ISteamUser/GetPlayerSummaries/v2/"


async def test_arbitrary_player_url_not_requested(plugin, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(plugin, "_get", request)
    with pytest.raises(ValueError):
        await plugin.player_profile("https://localhost/id/example")
    request.assert_not_called()


async def test_library_filter_sort_paging_and_units(plugin, monkeypatch):
    request = AsyncMock(
        return_value={
            "response": {
                "game_count": 3,
                "games": [
                    {"appid": 1, "name": "Portal A", "playtime_forever": 2},
                    {"appid": 2, "name": "Portal B", "playtime_forever": 200},
                    {"appid": 3, "name": "Other", "playtime_forever": 1000},
                ],
            }
        }
    )
    monkeypatch.setattr(plugin, "_get", request)
    result = await plugin.player_library(query="portal", limit=1)
    assert result["game_count"] == 3
    assert result["matched_count"] == 2
    assert result["games"][0]["appid"] == 2
    assert result["games"][0]["playtime_forever"] == 200
    assert result["next_offset"] == 1
    assert result["games"][0]["playtime_unit"] == "minutes"
    params = json.loads(request.call_args.args[2]["input_json"])
    assert params["include_played_free_games"] is True
    assert params["steamid"] == "76561198000000000"
    last = await plugin.player_library(query="portal", limit=1, offset=1)
    assert last["next_offset"] is None


@pytest.mark.parametrize("method", ["player_library", "player_recent"])
async def test_unavailable_is_not_empty(plugin, monkeypatch, method):
    monkeypatch.setattr(plugin, "_get", AsyncMock(return_value={"response": {}}))
    result = await getattr(plugin, method)()
    assert result["success"] is False


@pytest.mark.parametrize(
    "method,payload", [("player_library", {"game_count": 0}), ("player_recent", {"total_count": 0})]
)
async def test_explicit_empty_is_valid(plugin, monkeypatch, method, payload):
    monkeypatch.setattr(plugin, "_get", AsyncMock(return_value={"response": payload}))
    result = await getattr(plugin, method)()
    assert result["success"] is True
    assert result["games"] == []


async def test_reviews_current_endpoint_filter_and_cursor(plugin, monkeypatch):
    request = AsyncMock(
        return_value={
            "response": {
                "cursor": "next+/=",
                "reviews": [
                    {
                        "recommendationid": "123",
                        "author": {"steamid": "76561198000000000", "playtime_at_review": 60},
                        "voted_up": False,
                        "review": "<p>Bad &amp; slow</p>",
                    },
                ],
            }
        }
    )
    monkeypatch.setattr(plugin, "_get", request)
    result = await plugin.game_reviews(
        "620", review_type="negative", sort="recent", cursor="prev+/=", language="schinese"
    )
    assert request.call_args.args[1] == "/IUserReviewsService/GetAppReviews/v1/"
    params = json.loads(request.call_args.args[2]["input_json"])
    assert params["review_type"] == 2
    assert params["filter"] == 1
    assert params["cursor"] == "prev+/="
    assert "key" not in request.call_args.args[2]
    assert result["reviews"][0]["content"] == "Bad & slow"
    assert result["next_cursor"] == "next+/="
    assert result["summary"] == {}


async def test_key_required_only_for_player_queries(plugin, monkeypatch):
    plugin.set_plugin_config({})
    with pytest.raises(ValueError, match="api_key"):
        await plugin.player_profile("76561198000000000")
    monkeypatch.setattr(plugin, "_get", AsyncMock(return_value={"total": 0, "items": []}))
    assert (await plugin.search_games("Portal"))["success"]


async def test_missing_price_not_fabricated(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_get",
        AsyncMock(
            return_value={
                "620": {
                    "success": True,
                    "data": {
                        "name": "Example",
                        "is_free": False,
                        "short_description": "<p>Test</p>",
                    },
                }
            }
        ),
    )
    result = await plugin.game_details("620")
    assert "price_overview" not in result["details"]
    assert result["details"]["is_free"] is False


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_error_does_not_leak_key(plugin, monkeypatch, status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://api.steampowered.com/?key=test-secret"))
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=response))
    with pytest.raises(RuntimeError) as error:
        await plugin.player_profile()
    assert str(status) in str(error.value)
    assert "test-secret" not in str(error.value)


async def test_network_error_does_not_leak_key(plugin, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(side_effect=httpx.ConnectError("key=test-secret")))
    with pytest.raises(RuntimeError, match="ConnectError") as error:
        await plugin.player_profile()
    assert "test-secret" not in str(error.value)


def test_sdk_collects_seven_tools(plugin):
    assert len(plugin.get_components()) == 7


async def test_api_key_sent_in_header_only(plugin, monkeypatch):
    request = AsyncMock(return_value=httpx.Response(200, json={"response": {"players": []}}))
    monkeypatch.setattr(httpx.AsyncClient, "get", request)
    await plugin.player_profile()
    assert "key" not in request.call_args.kwargs["params"]
    assert request.call_args.kwargs["headers"]["x-webapi-key"] == "test-secret"


async def test_featured_bundles_are_not_game_ids(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin,
        "_get",
        AsyncMock(
            return_value={
                "top_sellers": {
                    "items": [
                        {"id": 620, "type": 0, "name": "Portal 2"},
                        {"id": 123, "type": 2, "name": "Bundle"},
                    ]
                }
            }
        ),
    )
    result = await plugin.browse_store("top_sellers")
    assert [item["id"] for item in result["items"]] == [620]
