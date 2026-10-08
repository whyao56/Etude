"""0.0.3 的「检索 + 画图并行」以及检索质量。

用户的原话是「图片生成质量不行，要生成图片和**检索图片或其他办法并行**」。
所以这里要盯的是三件事：

1. **真的是并行**，不是「先检索、失败才画图」。后者在检索慢的时候
   等于把画图的等待叠加上去。
2. **先到先得**。等的语义是「谁先给出合格的图谁赢」，
   不是「两个都等回来再择优」—— 检索 1–3 秒、画图 8–25 秒，
   等两个都到就等于把慢的那条加在快的那条身后。
3. **两条都不成时，报错要说清两边各自怎么失败的**。
   只说「没找到图片」的话，用户不知道该换关键词、该配画图模型、
   还是该关掉配图。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\xd9\x0e"
    b"\x1b\x0e"
    b"\x00\x00\x00\x0cIDATx\x9cc\xfc\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def paths(monkeypatch):
    """把两条路都换成可控的桩。

    **刻意不动 ``_provide_both`` 本身** —— 要测的正是它调度的方式，
    把它也换掉就等于什么都没测。
    """
    import app.providers.images as images_mod

    box = SimpleNamespace(
        mod=images_mod,
        search_delay=0.0,
        llm_delay=0.0,
        search_result=None,      # (bytes, ext) 或一个异常实例
        llm_result=None,
        ran=[],
        lock=threading.Lock(),
    )

    def record(which):
        with box.lock:
            box.ran.append(which)

    def fake_search(query, *, timeout=20.0):
        record("search")
        time.sleep(box.search_delay)
        if isinstance(box.search_result, Exception):
            raise box.search_result
        raw, ext = box.search_result
        return raw, ext, {"title": "t", "url": "u"}

    def fake_llm(prompt, cfg, timeout):
        record("llm")
        time.sleep(box.llm_delay)
        if isinstance(box.llm_result, Exception):
            raise box.llm_result
        return box.llm_result

    monkeypatch.setattr(images_mod, "search_image", fake_search)
    monkeypatch.setattr(images_mod, "_generate_llm", fake_llm)
    return box


def _provide(source="both", query="coffee shop", *, force=True, prompt="a prompt"):
    from app.providers import images

    return images.provide(
        query=query, prompt=prompt, source=source,
        gen_cfg={"base_url": "http://x/v1", "model": "m"}, force=force,
    )


def test_retrieval_wins_when_it_finishes_first(paths):
    """检索快，就该拿到检索的图 —— 而不是等画图也回来。"""
    paths.search_result = (PNG, ".png")
    paths.search_delay = 0.05
    paths.llm_result = (PNG, ".png")
    paths.llm_delay = 1.2

    t0 = time.monotonic()
    name = _provide()
    spent = time.monotonic() - t0

    assert name.startswith("search-"), f"检索先到，拿到的却是 {name}"
    # 关键断言：不能等画图。等的话这里至少 1.2 秒。
    assert spent < 0.7, f"耗了 {spent:.2f} 秒 —— 看起来是在等画图那条"


def test_the_two_paths_really_start_together(paths):
    """两条必须**同时**开跑。

    写成「先等检索，失败了再画图」的话，检索慢的时候（限流、超时）
    用户要干等两遍 —— 那就是把并行做成了串行。

    判定办法用 ``Barrier(2)``：只有两条同时在场才能通过。
    串行的话第一条会一直等到超时，整个用例会以 ImageError 失败。
    """
    barrier = threading.Barrier(2, timeout=5)

    def fake_search(query, *, timeout=20.0):
        barrier.wait()
        return PNG, ".png", {}

    def fake_llm(prompt, cfg, timeout):
        barrier.wait()
        return PNG, ".png"

    paths.mod.search_image = fake_search
    paths.mod._generate_llm = fake_llm

    name = _provide()
    assert name, "两条没能在同一个时刻碰头，说明它们是串行跑的"
    assert barrier.broken is False


def test_generation_covers_when_retrieval_fails(paths):
    """检索挂了（限流 / 搜不到），画图要接上场，而不是整个包没图。"""
    from app.providers import images

    paths.search_result = images.ImageError("检索被限流了。", "等会儿再试。")
    paths.llm_result = (PNG, ".png")
    paths.llm_delay = 0.05

    assert _provide().startswith("llm-")


def test_retrieval_covers_when_generation_fails(paths):
    """反过来也要成立。画图需要配密钥、需要余额，失败率比检索高得多。"""
    from app.providers import images

    paths.search_result = (PNG, ".png")
    paths.search_delay = 0.05
    paths.llm_result = images.ImageError("画图的密钥不对。", "去设置里检查。")

    assert _provide().startswith("search-")


def test_the_quick_one_wins_even_when_both_would_succeed(paths):
    """两条都能成时，赢的是**先到的**那条 —— 不是等两个都回来再择优。"""
    paths.search_result = (PNG, ".png")
    paths.search_delay = 1.5
    paths.llm_result = (PNG, ".png")
    paths.llm_delay = 0.05

    t0 = time.monotonic()
    name = _provide()
    spent = time.monotonic() - t0

    assert name.startswith("llm-")
    assert spent < 1.2, f"耗了 {spent:.2f} 秒 —— 看起来在等那条慢的检索"


def test_both_failing_says_what_each_side_did(paths):
    """两条都挂时，错误里必须**两边都提一句**。

    只写「没找到图片」的话，用户会一直换关键词 ——
    而真正的问题可能是画图模型没配。这两种毛病的下一步动作完全不同。
    """
    from app.providers import images

    paths.search_result = images.ImageError("Openverse 限流了。", "等会儿再试。")
    paths.llm_result = images.ImageError("没填画图模型名。", "去设置里填上。")

    with pytest.raises(images.ImageError) as exc:
        _provide()

    msg = str(exc.value)
    assert "图库检索" in msg and "画图模型" in msg, msg
    assert "限流" in msg and "模型名" in msg, msg


def test_the_winner_is_stored_under_its_own_source(paths):
    """``both`` 模式下，文件名仍用**实际命中的**来源前缀。

    统一写成 ``both-`` 的话，缓存就和单源模式对不上 ——
    用户从「both」切到「只用检索」时会重新下载一遍同样的图。
    """
    paths.search_result = (PNG, ".png")
    paths.search_delay = 0.02
    paths.llm_result = (PNG, ".png")
    paths.llm_delay = 1.0

    name = _provide()
    assert name.startswith("search-")

    # 切到单源模式：应该直接命中刚才那张缓存，不再走网络。
    paths.search_result = AssertionError("不该再下载一次")
    assert _provide(source="search", force=False) == name


def test_both_mode_reuses_the_drawn_picture_too(paths):
    """``both`` 也要能复用**画图**留下的缓存，而不只是检索的。"""
    from app.providers import images

    paths.search_result = images.ImageError("检索没了")
    paths.llm_result = (PNG, ".png")
    paths.llm_delay = 0.02

    name = _provide()
    assert name.startswith("llm-")

    paths.llm_result = AssertionError("不该再画一次")
    assert _provide(force=False) == name


def test_force_redraws_even_when_a_cache_exists(paths):
    """「重新生成配图」要能真的重来。

    不透传 force 的话，用户点「重新生成」会拿回同一张缓存，
    然后以为按钮坏了 —— 这类「点了没反应」是本项目最忌讳的。
    """
    paths.search_result = (PNG, ".png")
    paths.llm_result = (PNG, ".png")
    paths.llm_delay = 0.02
    first = _provide()

    paths.search_result = AssertionError("force=True 时不该读缓存")
    paths.llm_result = AssertionError("force=True 时不该读缓存")
    paths.search_delay = 0.0
    paths.llm_delay = 0.0
    # 两条都换成会抛的桩之后必然失败 —— 但只要它**没有**走缓存返回旧名字，
    # 就说明 force 生效了。
    from app.providers import images

    with pytest.raises(images.ImageError):
        _provide(force=True)
    assert first  # 上一张还在，只是这次需要重画


def test_an_unknown_source_reuses_the_search_cache(isolated_data, paths):
    """老配置 / 手改过的配置里可能写着别的词。

    一个拼错的来源名不该让**整个包没图** —— 退回检索，继续跑。
    这里用缓存来验证「退回的是检索」：如果退到别处，就命中不了。
    """
    paths.search_result = (PNG, ".png")
    seeded = _provide(source="search")

    paths.search_result = AssertionError("不该走网络")
    assert _provide(source="sarch", force=False) == seeded


def test_off_source_never_touches_the_network(paths):
    assert _provide(source="off") == ""
    assert _provide(source="none") == ""
    assert paths.ran == []


def test_the_catalog_puts_both_first(isolated_data):
    """界面第一个选项就是默认值。默认要是 ``both`` ——
    那正是用户要的「两路并行」。"""
    from app.providers import images

    ids = [s["id"] for s in images.IMAGE_SOURCES]
    assert ids[0] == "both"
    assert set(ids) == {"both", "search", "llm", "off"}


# --------------------------------------------------------------- 检索质量


def test_the_query_drops_the_half_that_is_speech_not_scenery():
    """破折号后面那半截是「你说了一句什么」，不是画面。

    ``At a concert, someone worries the crowd won't get it — you say it appeals to anyone.``
    前半截能搜到「演唱会人群」，后半截只能搜到一堆文章配图。
    """
    from app.providers import images

    q = images.build_query(
        "At a concert, someone worries the crowd won't get it — you say it appeals to anyone.",
        "It appeals to anyone.",
    )
    assert "concert" in q
    assert "appeals" not in q, f"破折号后面的部分没被砍掉：{q}"


def test_the_query_drops_mental_and_speech_verbs():
    """``worries`` / ``says`` / ``wonder`` 这类词在真实照片标题里基本不出现。

    带着它们去搜，命中的是「思考者雕塑」「演讲稿」这种不相干的图。
    """
    from app.providers import images

    q = images.build_query("A student worries about the exam and wonders what to do.", "")
    for word in ("worries", "wonders"):
        assert word not in q, f"心理动词没滤掉：{q}"


def test_contractions_are_reduced_to_their_stem():
    """缩写要拆成基词再判。

    ``won't`` 直接按词处理的话会是个谁都不认识的词；
    拆成 ``won`` 才能被「言说动词」那条规则挡住。
    """
    from app.providers import images

    q = images.build_query("Someone at the door won't leave.", "")
    assert "won't" not in q
    assert "won" not in q.split(), f"缩写没拆干净：{q}"


def test_the_query_never_falls_back_to_an_empty_string():
    """整句都是心理活动时，词表会空 —— 那时要退回原句，而不是发一个空查询。

    空查询在 Openverse 上不是「没有结果」，而是**返回一堆随机图**，
    表现成「每条都配上了完全不相干的照片」。
    """
    from app.providers import images

    q = images.build_query("He wondered whether he should say something.", "")
    assert q.strip(), "过滤之后什么都不剩时必须退回原始词表"


def test_title_hits_reward_a_candidate_whose_title_matches():
    """标题命中数要能真正影响排序。

    0.0.2 只看横图和分辨率，于是排序第一的经常是一张高清但不相干的图。
    """
    from app.providers import images

    words = {"coffee", "shop"}
    assert images._title_hits({"title": "Coffee shop interior"}, words) == 2
    assert images._title_hits({"title": "Abstract blue shapes"}, words) == 0
    assert images._title_hits({"tags": "coffee, shop, counter"}, words) == 2


def test_a_matching_title_beats_a_bigger_irrelevant_picture():
    """标题对得上，要赢过「更大但不相干」的那张。"""
    from app.providers import images

    relevant = {"url": "a", "width": 900, "height": 600, "title": "Coffee shop counter", "tags": ""}
    irrelevant = {"url": "b", "width": 4000, "height": 2600, "title": "Abstract wallpaper", "tags": ""}

    best = images.pick_candidate([irrelevant, relevant], "coffee shop")
    assert best["url"] == "a", "分辨率压过了相关性 —— 这就是「配图不相关」的来源"
