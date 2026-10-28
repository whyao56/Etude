/* 版本常量。
 *
 * 以前它写在 index.html 里，界面拆成模块之后放在这里 —— 因为
 * 「界面版本」现在属于界面代码，不属于壳。
 * 三处会读它，改版号时一起改：
 *   1. 本文件
 *   2. scripts/build_exe.py 的 preflight（读 app/__init__.py 的 __version__ 对比）
 *   3. scripts/make_release.py 的 assert_dist_version
 */
export const APP_VERSION = "0.1.1";
