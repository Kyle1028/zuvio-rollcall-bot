"""
Oracle Cloud ARM (VM.Standard.A1.Flex) 自動搶機腳本

Always Free 的 ARM 容量常常缺貨，手動按 Create 只會一直看到
「Out of capacity」。這支腳本會每隔幾分鐘自動嘗試建立實例，搶到就停，
並印出 Public IP（有設定 LINE 時也會推播通知）。

前置需求：
  1. pip install oci          （或 oci-cli，內含 SDK）
  2. ~/.oci/config 已設定好 API 金鑰（user / fingerprint / tenancy / region / key_file）
  3. SSH 公鑰檔（預設 ~/.ssh/zuvio_oracle.pub）

用法：
  python deploy/oci_launch_retry.py --check      # 只檢查設定、不建立任何資源
  python deploy/oci_launch_retry.py              # 開始自動重試（Ctrl+C 中止）
  python deploy/oci_launch_retry.py --interval 300 --ocpus 1 --memory 6
"""
import argparse
import datetime
import json
import os
import random
import sys
import time
import urllib.request

try:
    import oci
except ImportError:
    print("找不到 oci 套件，請先執行：pip install oci")
    sys.exit(1)


# ──────────────────────────────────────────────
# [1] 常數
# ──────────────────────────────────────────────
SHAPE = "VM.Standard.A1.Flex"
VCN_NAME = "zuvio-vcn"
SUBNET_NAME = "zuvio-subnet"
IGW_NAME = "zuvio-igw"
VCN_CIDR = "10.0.0.0/16"
SUBNET_CIDR = "10.0.0.0/24"

# 視為「暫時性、值得重試」的錯誤
CAPACITY_HINTS = ("out of host capacity", "out of capacity")

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ──────────────────────────────────────────────
# [2] 日誌與通知
# ──────────────────────────────────────────────
def log(msg: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def _load_line():
    """優先讀環境變數，其次讀專案根目錄的 settings.json（只取 LINE 欄位）。"""
    token = os.environ.get("ZUVIO_LINE_TOKEN")
    user_id = os.environ.get("ZUVIO_LINE_USER_ID")
    if token and user_id:
        return token, user_id
    path = os.path.join(REPO_DIR, "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("line_enabled") and cfg.get("line_token") and cfg.get("line_user_id"):
            return cfg["line_token"], cfg["line_user_id"]
    except Exception:
        pass
    return None, None


def notify(message: str):
    """用 LINE Messaging API 推播（未設定 LINE 時略過）。"""
    token, user_id = _load_line()
    if not (token and user_id):
        return
    try:
        req = urllib.request.Request(
            "https://api.line.me/v2/bot/message/push",
            data=json.dumps({
                "to": user_id,
                "messages": [{"type": "text", "text": message}],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        log(f"[LINE] 通知發送失敗：{e}")


# ──────────────────────────────────────────────
# [3] 資源查找／建立
# ──────────────────────────────────────────────
def find_image(compute, compartment_id: str) -> oci.core.models.Image:
    """找出相容 A1.Flex 的最新 Canonical Ubuntu 24.04（非 Minimal）映像。"""
    images = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    images = [i for i in images if "Minimal" not in i.display_name]
    if not images:
        raise RuntimeError("找不到相容 A1.Flex 的 Ubuntu 24.04 映像")
    return images[0]


def find_network(vnet, compartment_id: str):
    """回傳 (vcn, subnet)，不存在則回傳 None。"""
    vcns = vnet.list_vcns(compartment_id, display_name=VCN_NAME).data
    vcns = [v for v in vcns if v.lifecycle_state == "AVAILABLE"]
    if not vcns:
        return None, None
    vcn = vcns[0]
    subnets = vnet.list_subnets(compartment_id, vcn_id=vcn.id, display_name=SUBNET_NAME).data
    subnets = [s for s in subnets if s.lifecycle_state == "AVAILABLE"]
    return vcn, (subnets[0] if subnets else None)


def ensure_network(vnet, compartment_id: str):
    """確保有一組可上網的 VCN + public subnet；缺什麼補什麼（可重複執行）。"""
    vcn, subnet = find_network(vnet, compartment_id)

    if vcn is None:
        log(f"[網路] 建立 VCN {VCN_NAME} ...")
        vcn = vnet.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=compartment_id,
            cidr_blocks=[VCN_CIDR],
            display_name=VCN_NAME,
            dns_label="zuviovcn",
        )).data
        vcn = oci.wait_until(vnet, vnet.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE").data

    # Internet Gateway
    igws = vnet.list_internet_gateways(compartment_id, vcn_id=vcn.id).data
    igws = [g for g in igws if g.lifecycle_state == "AVAILABLE"]
    if igws:
        igw = igws[0]
    else:
        log(f"[網路] 建立 Internet Gateway {IGW_NAME} ...")
        igw = vnet.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment_id,
            vcn_id=vcn.id,
            is_enabled=True,
            display_name=IGW_NAME,
        )).data
        igw = oci.wait_until(vnet, vnet.get_internet_gateway(igw.id), "lifecycle_state", "AVAILABLE").data

    # 預設路由表加上 0.0.0.0/0 → IGW
    rt = vnet.get_route_table(vcn.default_route_table_id).data
    if not any(r.destination == "0.0.0.0/0" for r in rt.route_rules):
        log("[網路] 設定預設路由 0.0.0.0/0 → Internet Gateway ...")
        rules = list(rt.route_rules) + [oci.core.models.RouteRule(
            destination="0.0.0.0/0",
            destination_type="CIDR_BLOCK",
            network_entity_id=igw.id,
        )]
        vnet.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))

    # Public subnet（使用 VCN 預設安全清單：預設即允許 SSH 22 進站）
    if subnet is None:
        log(f"[網路] 建立 public subnet {SUBNET_NAME} ...")
        subnet = vnet.create_subnet(oci.core.models.CreateSubnetDetails(
            compartment_id=compartment_id,
            vcn_id=vcn.id,
            cidr_block=SUBNET_CIDR,
            display_name=SUBNET_NAME,
            dns_label="zuviosub",
            prohibit_public_ip_on_vnic=False,
            route_table_id=vcn.default_route_table_id,
            security_list_ids=[vcn.default_security_list_id],
        )).data
        subnet = oci.wait_until(vnet, vnet.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE").data

    return subnet


def find_existing_instance(compute, compartment_id: str, name: str):
    """避免重複建立：同名且未終止的實例視為已存在。"""
    instances = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment_id, display_name=name
    ).data
    alive = [i for i in instances if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
    return alive[0] if alive else None


def get_public_ip(compute, vnet, compartment_id: str, instance_id: str):
    attachments = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
    for att in attachments:
        if att.lifecycle_state == "ATTACHED":
            vnic = vnet.get_vnic(att.vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
    return None


# ──────────────────────────────────────────────
# [4] 主流程
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="自動重試建立 Oracle A1.Flex 實例")
    parser.add_argument("--profile", default="DEFAULT", help="~/.oci/config 的 profile 名稱")
    parser.add_argument("--name", default="zuvio-bot", help="實例名稱")
    parser.add_argument("--ocpus", type=float, default=1, help="OCPU 數（Always Free 上限合計 4）")
    parser.add_argument("--memory", type=float, default=6, help="記憶體 GB（Always Free 上限合計 24）")
    parser.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/zuvio_oracle.pub"),
                        help="SSH 公鑰檔路徑")
    parser.add_argument("--interval", type=int, default=180, help="每輪重試間隔秒數（預設 180）")
    parser.add_argument("--check", action="store_true", help="只檢查設定，不建立任何資源")
    args = parser.parse_args()

    config = oci.config.from_file(profile_name=args.profile)
    oci.config.validate_config(config)
    compartment_id = config["tenancy"]  # 直接用 root compartment

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    vnet = oci.core.VirtualNetworkClient(config)

    with open(args.ssh_key, "r", encoding="utf-8") as f:
        ssh_pub = f.read().strip()

    ads = [ad.name for ad in identity.list_availability_domains(compartment_id).data]
    image = find_image(compute, compartment_id)
    log(f"[設定] 區域 {config['region']}；可用 AD：{', '.join(ads)}")
    log(f"[設定] 映像：{image.display_name}")
    log(f"[設定] 規格：{SHAPE} {args.ocpus:g} OCPU / {args.memory:g} GB；名稱 {args.name}")

    existing = find_existing_instance(compute, compartment_id, args.name)
    if existing:
        log(f"[略過] 已有同名實例 {args.name}（狀態 {existing.lifecycle_state}），不重複建立。")
        ip = get_public_ip(compute, vnet, compartment_id, existing.id)
        if ip:
            log(f"[資訊] Public IP：{ip}")
        return 0

    if args.check:
        vcn, subnet = find_network(vnet, compartment_id)
        log(f"[檢查] 網路：VCN {'已存在' if vcn else '尚未建立（正式執行時會自動建立）'}，"
            f"subnet {'已存在' if subnet else '尚未建立'}")
        log("[檢查] 設定與權限皆正常，可以正式執行。")
        return 0

    subnet = ensure_network(vnet, compartment_id)
    log(f"[網路] 使用 subnet {subnet.display_name}")

    launch_base = dict(
        compartment_id=compartment_id,
        display_name=args.name,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=args.ocpus, memory_in_gbs=args.memory,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image.id),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet.id, assign_public_ip=True,
        ),
        metadata={"ssh_authorized_keys": ssh_pub},
    )

    attempt = 0
    while True:
        wait = args.interval
        for ad in ads:
            attempt += 1
            try:
                log(f"[第 {attempt} 次] 嘗試在 {ad} 建立實例 ...")
                inst = compute.launch_instance(
                    oci.core.models.LaunchInstanceDetails(availability_domain=ad, **launch_base)
                ).data
            except oci.exceptions.ServiceError as e:
                msg = (e.message or "").lower()
                if e.status == 500 or any(h in msg for h in CAPACITY_HINTS):
                    log(f"  → 容量不足（{e.code}），稍後重試。")
                    continue
                if e.status == 429:
                    wait = max(args.interval * 2, 300)
                    log(f"  → 請求過於頻繁（429），下一輪延長等待至 {wait} 秒。")
                    break
                if e.code == "LimitExceeded":
                    log(f"  → 超出帳號配額：{e.message}")
                    log("     （Always Free A1 合計上限 4 OCPU / 24 GB；請檢查是否已有其他 A1 實例）")
                    return 1
                log(f"  → 非預期錯誤 {e.status} {e.code}：{e.message}")
                return 1

            # 成功送出建立請求
            log(f"★ 搶到了！實例 {inst.display_name} 建立中，等待啟動 ...")
            inst = oci.wait_until(
                compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING",
                max_wait_seconds=900,
            ).data
            ip = get_public_ip(compute, vnet, compartment_id, inst.id)
            log(f"★ 實例已 RUNNING。Public IP：{ip or '（尚未取得，請到 Console 查看）'}")
            log(f"  連線指令：ssh -i ~/.ssh/zuvio_oracle ubuntu@{ip}")
            notify(f"✅ Oracle A1 實例搶到了！\n名稱：{inst.display_name}\nPublic IP：{ip}")
            return 0

        jitter = random.randint(0, 30)
        log(f"本輪失敗，{wait + jitter} 秒後再試（Ctrl+C 可中止）。")
        time.sleep(wait + jitter)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("已手動中止。")
        sys.exit(130)
