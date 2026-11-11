"""Import vendor candidates from an editable UTF-8 CSV (unit prices in copper)."""
import csv
from common import AppError, connect, ensure_items, load_config, main_guard, parser, run, utcnow
from static_store import offer
import json


def main():
    p = parser(__doc__)
    p.add_argument("csv_file")
    args = p.parse_args()
    cfg = load_config(args.config)
    rows = []
    with open(args.csv_file, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not {"item_id", "source", "note"} <= set(reader.fieldnames or []):
            raise AppError("CSV 필수 헤더: item_id,source,note. 여러 판매처에는 offer_key를 쓰세요.")
        for row in reader:
            item_id=int(row['item_id'])
            if item_id<=0 or not row['source'].strip(): raise AppError('item_id와 출처가 필요합니다.')
            values={k:row[k] for k in ('note','npc_name','zone','faction','source_url','reputation_required',
                'reputation_discount_faction','conditions','stock_state','price_basis','game_build_override') if row.get(k)}
            for k in ('npc_id','map_id','price_copper','purchase_qty','stock_quantity','vendor_price'):
                if row.get(k):
                    values[k]=int(row[k])
                    if values[k]<0 or (k in ('npc_id','purchase_qty') and values[k]==0): raise AppError('수량/가격 범위 오류')
            for k in ('x','y'):
                if row.get(k):
                    values[k]=float(row[k])
                    if not 0<=values[k]<=100: raise AppError('지도 좌표는 0~100입니다.')
            costs=row.get('currency_costs')
            if costs:
                if not isinstance(json.loads(costs),list): raise AppError('currency_costs는 JSON 배열입니다. 빈칸은 미확인.')
                values['currency_costs']=costs
            if 'price_copper' in values:
                amount=values['price_copper'];values.update(gold=amount//10000,silver=amount//100%100,copper=amount%100)
                values['vendor_price']=None # Avoid truncating fractional copper per-item bundle costs.
                values['price_basis']=row.get('price_basis') or 'per_purchase'
            elif cfg['game_flavor']=='forever' and 'vendor_price' in values:
                raise AppError('Forever CSV에는 price_copper(한 번 구매 가격)와 purchase_qty를 사용하세요.')
            else:
                values['price_basis']='legacy_unit_candidate'
            values['verification']='manual_unverified'
            values['game_version']=cfg['game_flavor']
            rows.append((item_id,'csv:'+row['source'].strip(),row.get('offer_key') or f'legacy:{item_id}',values))
    db = connect(cfg, args.db, mode=None)
    with run(db, "import_vendors"):
        with db:
            ensure_items(db, (r[0] for r in rows))
            for item_id,source,key,values in rows:
                offer(db,item_id,source,key,**values)
        print(f"상점 후보 {len(rows)}개 반영 (단가 0은 분석에서 사용 안 함)")


if __name__ == "__main__":
    main_guard(main)
