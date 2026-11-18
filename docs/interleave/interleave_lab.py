#!/usr/bin/env python3
"""真实交错实验：在线建立关系/索引与活动事务的相互影响。

仅使用 Python 标准库和公开 HTTP 接口；每个场景使用独立临时实例。
运行：python3 -I docs/interleave/interleave_lab.py --binary /tmp/lamina-build/laminad
输出：每条交错步骤的真实请求、HTTP 状态与关键字段，供操作手册引用。
本脚本不修改任何产品代码，也不引入新的数据库能力。
"""
import argparse, json, os, pathlib, signal, socket, subprocess, tempfile, time
import urllib.request, urllib.error

PREFIX = "/v1/lamina"
SCHEMA = {"name": "spectra", "primary": "sample", "columns": [
    {"name": "sample", "type": "int"},
    {"name": "band", "type": "text"},
    {"name": "amplitude", "type": "int", "nullable": True},
]}


def row(n, band=None, amplitude=None):
    return {"sample": n, "band": band or f"b{n}",
            "amplitude": n * 10 if amplitude is None else amplitude}


class Server:
    def __init__(self, binary, root):
        self.binary = binary
        self.root = pathlib.Path(root)
        self.proc = None
        self.log = None

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}{PREFIX}"
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            [self.binary, "-data", str(self.root), "-listen", f"127.0.0.1:{port}"],
            stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.log.seek(0)
                raise RuntimeError(self.log.read().decode())
            try:
                self.call("GET", "/health")
                return self
            except (OSError, urllib.error.URLError):
                time.sleep(0.03)
        raise RuntimeError("service did not become ready")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.log:
            self.log.close()
            self.log = None

    def call(self, method, path, data=None, status=None):
        encoded = None if data is None else json.dumps(data, ensure_ascii=False).encode()
        req = urllib.request.Request(
            self.url + path, data=encoded, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                code, body = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read()
        parsed = None
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = body.decode(errors="replace")
        if status is not None and code != status:
            raise AssertionError(f"{method} {path}: 期望 {status}，实际 {code}: {body.decode()}")
        return code, parsed


class Lab:
    def __init__(self, binary):
        self.binary = binary
        self.tmp = tempfile.TemporaryDirectory(prefix="lamina-interleave-")
        self.root = pathlib.Path(self.tmp.name) / "data"
        self.s = Server(binary, self.root).start()
        self.steps = []

    def close(self):
        self.s.stop()
        self.tmp.cleanup()

    def note(self, title, method, path, code, body, expect=None):
        self.steps.append({"step": title, "request": f"{method} {path}",
                           "http": code, "expect": expect, "body": body})

    def do(self, title, method, path, data=None, status=200):
        code, body = self.s.call(method, path, data)
        self.note(title, method, path, code, body, expect=status)
        assert code == status, f"{title}: 期望 {status} 得 {code}: {body}"
        return body

    def begin(self, title, readonly=False):
        body = self.do(title, "POST", "/transactions", {"read_only": readonly}, 201)
        return body["id"]

    def put(self, title, tx, r, insert=False, status=200):
        return self.do(title, "POST", f"/transactions/{tx}/put",
                       {"relation": "spectra", "row": r, "insert_only": insert}, status)

    def commit(self, title, tx, status=200):
        return self.do(title, "POST", f"/transactions/{tx}/commit", status=status)

    def rollback(self, title, tx):
        return self.do(title, "POST", f"/transactions/{tx}/rollback", status=200)

    def query(self, title, payload, status=200):
        return self.do(title, "POST", "/query", payload, status)

    def sql(self, title, text, tx=None, status=200):
        payload = {"sql": text}
        if tx:
            payload["transaction"] = tx
        return self.do(title, "POST", "/sql", payload, status)

    def create_index(self, title, name, column, unique=False, status=201):
        return self.do(title, "POST", "/indexes",
                       {"name": name, "relation": "spectra", "column": column,
                        "unique": unique}, status)

    def create_relation(self, title, name, status=201):
        schema = {"name": name, "primary": "k",
                  "columns": [{"name": "k", "type": "int"},
                              {"name": "v", "type": "text", "nullable": True}]}
        return self.do(title, "POST", "/relations", schema, status)

    def stats(self, title):
        return self.do(title, "GET", "/stats")

    def catalog(self, title):
        return self.do(title, "GET", "/catalog")

    def changes(self, title, after, limit=20):
        return self.do(title, "GET", f"/changes?after={after}&limit={limit}")

    def vacuum(self, title, horizon, status=200):
        return self.do(title, "POST", "/vacuum", {"horizon": horizon}, status)

    def seed(self):
        self.do("建立 spectra 关系", "POST", "/relations", SCHEMA, 201)
        tx = self.begin("种子事务开始")
        for n in range(1, 4):
            self.put(f"种子写入 sample={n}", tx, row(n))
        out = self.commit("种子提交", tx)
        return out["sequence"]


def scenario_a(binary):
    """A: 索引建立前后，旧事务/新事务/历史序号各自看到哪个目录。"""
    lab = Lab(binary)
    try:
        seed_seq = lab.seed()
        old = lab.begin("旧读写事务 T_old 开始（固定目录快照）")
        ro = lab.begin("旧只读事务 T_ro 开始", readonly=True)
        before = lab.stats("建索引前 /stats")
        lab.create_index("在线建立非唯一索引 by_amplitude", "by_amplitude", "amplitude")
        after = lab.stats("建索引后 /stats（活动事务仍在）")
        cat = lab.catalog("当前目录 /catalog")
        where = [{"column": "amplitude", "op": "=", "value": 20}]
        q_old = lab.query("T_old 同条件查询（含新索引列）",
                          {"relation": "spectra", "where": where,
                           "transaction": old})
        q_ro = lab.query("T_ro 同条件查询",
                         {"relation": "spectra", "where": where, "transaction": ro})
        q_now = lab.query("当前普通读同条件查询",
                          {"relation": "spectra", "where": where})
        q_at = lab.query(f"按序号 at={seed_seq} 历史查询",
                         {"relation": "spectra", "where": where, "at": seed_seq})
        q_sql = lab.sql("T_old 走 SQL 入口同条件查询（同快照同计划）",
                        "SELECT sample FROM spectra WHERE amplitude = 20;", tx=old)
        lab.put("T_old 继续写入 sample=4（旧关系仍可改）", old, row(4))
        lab.commit("T_old 提交成功", old)
        lab.rollback("T_ro 撤销", ro)
        return {"scenario": "A 目录快照与查询计划边界", "seed_sequence": seed_seq,
                "stats_before_active": before["active"],
                "stats_after_active": after["active"],
                "catalog_indexes": cat["indexes"],
                "old_tx_plan": q_old["plan"], "ro_tx_plan": q_ro["plan"],
                "current_plan": q_now["plan"], "at_plan": q_at["plan"],
                "old_sql_plan": q_sql["plan"],
                "steps": lab.steps}
    finally:
        lab.close()


def scenario_b(binary):
    """B: 建唯一索引时旧事务暂存了重复值——构建成功但提交失败。"""
    lab = Lab(binary)
    try:
        lab.seed()
        t_dup = lab.begin("T_dup 开始")
        lab.put("T_dup 暂存 sample=4 band=shared", t_dup, row(4, band="shared"))
        lab.put("T_dup 暂存 sample=5 band=shared（暂存内重复）",
                t_dup, row(5, band="shared"))
        t_ok = lab.begin("T_ok 开始")
        lab.put("T_ok 暂存 sample=6 band=unique6", t_ok, row(6, band="unique6"))
        lab.create_index("对 band 建唯一索引（只看已提交数据，构建成功）",
                         "by_band_uq", "band", unique=True)
        cat = lab.catalog("构建后目录含唯一索引")
        lab.commit("T_dup 提交：唯一冲突 409，事务闭合", t_dup, status=409)
        lab.commit("T_dup 再次提交：404（闭合不可复用）", t_dup, status=404)
        ok_result = lab.commit("T_ok 提交：不冲突，成功", t_ok)
        stats = lab.stats("失败后 /stats：无残留活动事务")
        rows = lab.query("当前读数确认 T_dup 写集未泄漏",
                         {"relation": "spectra", "where": [
                             {"column": "band", "op": "=", "value": "shared"}]})
        return {"scenario": "B 后建唯一索引否决旧事务提交",
                "catalog_indexes": cat["indexes"], "ok_commit": ok_result,
                "active_after": stats["active"], "shared_rows": rows["rows"],
                "steps": lab.steps}
    finally:
        lab.close()


def scenario_c(binary):
    """C: 对照——唯一索引先建，两个并发新事务抢同一值。"""
    lab = Lab(binary)
    try:
        lab.seed()
        lab.create_index("先建唯一索引 by_band_uq", "by_band_uq", "band", unique=True)
        a = lab.begin("T_a 开始（索引已在其快照中）")
        b = lab.begin("T_b 开始")
        lab.put("T_a 暂存 sample=4 band=shared", a, row(4, band="shared"))
        lab.put("T_b 暂存 sample=5 band=shared", b, row(5, band="shared"))
        lab.commit("T_a 先提交成功", a)
        lab.commit("T_b 提交：409 唯一冲突并闭合", b, status=409)
        swap = lab.begin("T_swap 开始：做唯一值互换")
        lab.put("T_swap 把 sample=1 改为 band=b2", swap, row(1, band="b2"))
        lab.put("T_swap 把 sample=2 改为 band=b1", swap, row(2, band="b1"))
        lab.commit("事务内互换唯一值：提交成功", swap)
        coll = lab.begin("T_coll 开始：改成现存他行的值")
        lab.put("T_coll 把 sample=3 改为 band=b1（与 sample=2 现状冲突）",
                coll, row(3, band="b1"))
        lab.commit("T_coll 提交：按最终状态判 409", coll, status=409)
        return {"scenario": "C 索引先建时的提交期约束（对照）", "steps": lab.steps}
    finally:
        lab.close()


def scenario_d(binary):
    """D: 同键 first-committer-wins 与索引是否存在无关。"""
    lab = Lab(binary)
    try:
        lab.seed()
        x = lab.begin("T_x 开始（无索引时期）")
        y = lab.begin("T_y 开始（无索引时期）")
        lab.put("T_x 暂存 sample=1 amplitude=111", x, row(1, amplitude=111))
        lab.put("T_y 暂存 sample=1 amplitude=222", y, row(1, amplitude=222))
        lab.put("T_y 附带暂存 sample=7", y, row(7))
        lab.create_index("交错中建立非唯一索引 by_amplitude", "by_amplitude", "amplitude")
        lab.commit("T_x 先提交成功", x)
        lab.commit("T_y 提交：同键写冲突 409，附带写入一并丢弃", y, status=409)
        rows = lab.query("当前读数：sample=7 不存在，sample=1 是 T_x 的值",
                         {"relation": "spectra", "order": [
                             {"column": "sample", "desc": False}]})
        return {"scenario": "D 写冲突不依赖索引且整笔回滚",
                "rows": rows["rows"], "steps": lab.steps}
    finally:
        lab.close()


def scenario_e(binary):
    """E: 后建关系对旧事务不可见；重启后事务不复活。"""
    lab = Lab(binary)
    try:
        seed_seq = lab.seed()
        old = lab.begin("T_old 在新关系之前开始")
        rel = lab.create_relation("在线建立新关系 readings", "readings")
        lab.do("T_old 写新关系：409 目录快照之外", "POST",
               f"/transactions/{old}/put",
               {"relation": "readings", "row": {"k": 1, "v": "x"}}, status=409)
        lab.query("T_old 查新关系：410 该序号关系不存在",
                  {"relation": "readings", "transaction": old}, status=410)
        fresh = lab.begin("新事务 T_new 开始")
        lab.do("T_new 写新关系成功", "POST", f"/transactions/{fresh}/put",
               {"relation": "readings", "row": {"k": 1, "v": "x"}}, status=200)
        lab.commit("T_new 提交", fresh)
        lab.query(f"at={seed_seq} 查新关系：410",
                  {"relation": "readings", "at": seed_seq}, status=410)
        lab.put("T_old 仍可改旧关系 spectra", old, row(4))
        active = lab.stats("重启前 /stats：两个活动事务")
        chg = lab.changes("变更流包含 relation 记录", after=seed_seq)
        lab.s.stop()
        lab.s.start()
        stats2 = lab.stats("重启后 /stats：活动事务清零（私有写集不持久化）")
        cat2 = lab.catalog("重启后目录：关系 created 序号不变")
        plan = lab.query("重启后查询正常（该场景无索引，计划为 scan）",
                         {"relation": "spectra", "where": [
                             {"column": "amplitude", "op": "=", "value": 10}]})
        lab.do("T_old 复活尝试：404", "POST", f"/transactions/{old}/commit", status=404)
        gone = lab.query("T_old 未提交的 sample=4 不存在",
                         {"relation": "spectra", "where": [
                             {"column": "sample", "op": "=", "value": 4}]})
        return {"scenario": "E 新关系可见性与重启核对",
                "relation_created": rel["created"],
                "active_before_restart": [t["id"] for t in active["active"]],
                "change_kinds": [r["kind"] for r in chg["records"]],
                "active_after_restart": stats2["active"],
                "relations_after": cat2["relations"],
                "plan_after": plan["plan"], "ghost_rows": gone["rows"],
                "steps": lab.steps}
    finally:
        lab.close()


def scenario_f(binary):
    """F: 活动读者钉住回收水位；唯一索引在重启后仍被重放并强制。"""
    lab = Lab(binary)
    try:
        seed_seq = lab.seed()
        ro = lab.begin("长只读事务 T_ro 固定旧快照", readonly=True)
        writer = lab.begin("T_w 开始更新旧行")
        lab.put("T_w 更新 sample=1 amplitude=999", writer, row(1, amplitude=999))
        lab.commit("T_w 提交", writer)
        head = lab.stats("取当前 head")["sequence"]
        lab.create_index("建立唯一索引 by_band_uq", "by_band_uq", "band", unique=True)
        pinned = lab.vacuum(f"请求水位 {head} 的 vacuum，被 T_ro 钉住", head)
        lab.query("T_ro 仍读到旧值 amplitude=10",
                  {"relation": "spectra", "where": [
                      {"column": "sample", "op": "=", "value": 1}],
                   "transaction": ro})
        lab.rollback("T_ro 关闭", ro)
        reclaimed = lab.vacuum("关闭后再次 vacuum 推进水位", head)
        lab.query("低于水位的历史读：410",
                  {"relation": "spectra", "at": seed_seq}, status=410)
        lab.do("重启前 checkpoint", "POST", "/checkpoint")
        lab.s.stop()
        lab.s.start()
        cat = lab.catalog("重启后唯一索引仍在目录")
        t = lab.begin("重启后新事务")
        lab.put("新事务暂存与现存 band 冲突的行", t, row(9, band="b2"))
        lab.commit("重启后唯一约束仍强制：409", t, status=409)
        stats = lab.stats("重启后计数归零但数据仍在")
        return {"scenario": "F 回收钉住与重启后约束核对",
                "pinned": pinned, "reclaimed": reclaimed,
                "indexes_after_restart": cat["indexes"],
                "commits_since_open": stats["commits_since_open"],
                "keys": stats["keys"], "steps": lab.steps}
    finally:
        lab.close()


def scenario_g(binary):
    """G: 唯一索引构建失败不发布目录、不推进序号；重复 DDL 拒绝。"""
    lab = Lab(binary)
    try:
        lab.seed()
        tx = lab.begin("写入两行相同 band 的已提交数据")
        lab.put("sample=4 band=dup", tx, row(4, band="dup"))
        lab.put("sample=5 band=dup", tx, row(5, band="dup"))
        lab.commit("提交重复数据", tx)
        before = lab.stats("失败构建前 head 与目录")
        lab.create_index("对含重复值的 band 建唯一索引：409 且不发布",
                         "bad_unique", "band", unique=True, status=409)
        after = lab.stats("失败构建后 head 不变")
        cat = lab.catalog("目录中没有 bad_unique")
        lab.create_index("先建非唯一同名索引 bad_unique", "bad_unique", "amplitude")
        lab.create_index("重复名称再建：400", "bad_unique", "band", status=400)
        lab.create_index("引用不存在的列：400", "ghost_col", "nope", status=400)
        lab.create_relation("重复关系名 spectra：409", "spectra", status=409)
        return {"scenario": "G 失败 DDL 的原子性与重复请求",
                "head_before": before["sequence"], "head_after": after["sequence"],
                "indexes": cat["indexes"], "steps": lab.steps}
    finally:
        lab.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--scenario", default="all",
                    choices=["all", "A", "B", "C", "D", "E", "F", "G"])
    args = ap.parse_args()
    scenarios = {"A": scenario_a, "B": scenario_b, "C": scenario_c,
                 "D": scenario_d, "E": scenario_e, "F": scenario_f,
                 "G": scenario_g}
    picked = list(scenarios) if args.scenario == "all" else [args.scenario]
    report = []
    for key in picked:
        report.append(scenarios[key](args.binary))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
