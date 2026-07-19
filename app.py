import streamlit as st
import folium
from streamlit_folium import st_folium
import json
import pandas as pd
import re
import os
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader
# Import các Module tự viết từ dự án gốc của bạn
from api_client import CCTSClient
from utils import extract_core_station_code, parse_duration_to_hours
import subprocess

def install_playwright():
    if not os.path.exists("/home/appuser/.cache/ms-playwright"):
        print("Đang tải trình duyệt Playwright, vui lòng đợi trong vài giây...")
        subprocess.run(["playwright", "install", "chromium"])
        print("Tải xong!")

# Gọi hàm này trước khi bắt đầu logic chính của app
install_playwright()

st.set_page_config(layout="wide", page_title="CCTS Map")
# 1. Load config đăng nhập
with open('config.yaml') as file:
    config = yaml.load(file, Loader=SafeLoader)

authenticator = stauth.Authenticate(
    config['credentials'],
    config['cookie']['name'],
    config['cookie']['key'],
    config['cookie']['expiry_days']
)
# ==========================================
# ⚙️ 1. CẤU HÌNH TRANG & BẢO MẬT
# ==========================================
st.set_page_config(page_title="Charging Station Map", layout="wide")

# Lấy thông tin đăng nhập từ Streamlit Secrets (thiết lập trên Streamlit Cloud)
try:
    CCTS_USER = st.secrets["CCTS_USERNAME"]
    CCTS_PASS = st.secrets["CCTS_PASSWORD"]
except KeyError:
    st.error("⚠️ Chưa cấu hình thông tin đăng nhập trong Streamlit Secrets!")
    st.stop()

# ==========================================
# 🛠️ 2. MODULE XỬ LÝ DỮ LIỆU TĨNH (JSON)
# ==========================================
@st.cache_data
def load_static_data():
    """Tải và parse file JSON 1 lần duy nhất để tối ưu bộ nhớ"""
    coords_map = {}
    tech_map = {}

    # 1. Xây dựng dictionary Tọa độ từ station_info.json
    try:
        with open("station_info.json", 'r', encoding='utf-8') as f:
            station_data = json.load(f)
            for entry in station_data:
                store_id = entry.get("store_id")
                lat = entry.get("lat")
                lng = entry.get("lng")
                if store_id and lat and lng:
                    core_code = extract_core_station_code(store_id)
                    coords_map[core_code] = {'lat': float(lat), 'lng': float(lng)}
    except Exception as e:
        st.warning(f"Lỗi đọc station_info.json: {e}")

    # 2. Xây dựng dictionary Kỹ thuật viên từ list_Stations.json
    try:
        with open("list_Stations.json", 'r', encoding='utf-8') as f:
            list_stations = json.load(f)
            for region, engs in list_stations.items():
                for eng, stations in engs.items():
                    for st in stations:
                        core_code = extract_core_station_code(st)
                        tech_map[core_code] = eng
    except Exception as e:
        st.warning(f"Lỗi đọc list_Stations.json: {e}")

    return coords_map, tech_map

# ==========================================
# 🔄 3. MODULE FETCH API (CÓ CACHE)
# ==========================================
@st.cache_data(ttl=300) # Làm mới dữ liệu tự động sau mỗi 5 phút
def fetch_live_tickets():
    """Gọi CCTS API để lấy payload trực tiếp"""
    client = CCTSClient(username=CCTS_USER, password=CCTS_PASS)
    try:
        client.login()
        # Lấy dữ liệu mở với các status mục tiêu như trong auto_ccts.py
        target_statuses = ["Open", "Appointment", "Pending for spare parts", "Pending for ASP close"]
        dfs = client.export_and_download_tickets(
            start_time="2026-05-01 00:00:00",
            end_time=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            ticket_status=target_statuses
        )

        if not dfs or "Ticket Information" not in dfs:
            return pd.DataFrame()

        df = dfs["Ticket Information"].copy()
        
        # Lọc bỏ các mô tả lỗi bắt đầu bằng "BSS.No2" theo logic cũ
        if "Problem Description" in df.columns:
            df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()
        
        # Chỉ giữ lại các cột cần thiết cho payload
        cols_needed = ["Ticket ID", "Ticket Status", "Station Code", "Charge Point ID", "Ticket Duration", "Problem Description"]
        # Đảm bảo các cột tồn tại để tránh lỗi KeyError
        for col in cols_needed:
            if col not in df.columns:
                df[col] = ""
                
        return df[cols_needed].drop_duplicates(subset=["Ticket ID"])

    except Exception as e:
        st.error(f"Lỗi khi gọi API CCTS: {e}")
        return pd.DataFrame()

# ==========================================
# 🎨 4. MODULE RENDER BẢN ĐỒ
# ==========================================
def get_marker_color(duration_str):
    """Phân loại màu Marker dựa trên số giờ SLA"""
    try:
        hours = parse_duration_to_hours(duration_str)
        if hours > 48:
            return "darkred"
        elif hours >= 24:
            return "orange"
        else:
            return "green"
    except:
        return "gray"

def create_station_popup_html(station_code, tickets_df, tech_name):
    # Loại bỏ max-height và overflow-y ở thẻ div chính
    html_content = f"""
    <div style="font-family: Arial; font-size: 12px; min-width: 260px; padding: 5px;">
        <h4 style="margin: 0 0 5px 0; color: #1f77b4; font-size: 14px;">Trạm: {station_code}</h4>
        <div style="margin-bottom: 10px;">
            <b>Kỹ thuật viên:</b> <span style="color: #2ca02c; font-weight: bold;">{tech_name}</span>
        </div>
        <hr style="margin: 5px 0;">
    """

    for _, row in tickets_df.iterrows():
        cp_id = str(row['Charge Point ID'])
        is_bss = cp_id.startswith("BSS")
        card_bg = "#eef6ff" if is_bss else "#f9f9f9"
        icon = "🔋 BSS" if is_bss else "⚡ Sạc"
        
        html_content += f"""
        <div style="background-color: {card_bg}; padding: 6px; margin-bottom: 6px; border-radius: 4px; border-left: 3px solid #1f77b4;">
            <b style="color: #1f77b4; font-size: 11px;">{icon} - {cp_id}</b><br>
            <div style="font-size: 11px;">
                <b>ID:</b> {row['Ticket ID']} | <b>TT:</b> {row['Ticket Status']}<br>
                <b>Thời gian:</b> <span style="color: #d62728; font-weight: bold;">{row['Ticket Duration']}</span><br>
                <i style="color: #555;">{row['Problem Description']}</i>
            </div>
        </div>
        """

    html_content += "</div>"
    return html_content

@st.cache_data
def load_static_data():
    """Tải và parse file JSON và Excel 1 lần duy nhất để tối ưu bộ nhớ"""
    coords_map = {}
    tech_map = {}
    region_map = {} # Thêm biến lưu khu vực quản lý

    # 1. Đọc JSON lấy tọa độ (Ưu tiên 1)
    try:
        with open("station_info.json", 'r', encoding='utf-8') as f:
            station_data = json.load(f)
            for entry in station_data:
                store_id = entry.get("store_id")
                lat = entry.get("lat")
                lng = entry.get("lng")
                if store_id and lat and lng:
                    core_code = extract_core_station_code(store_id)
                    coords_map[core_code] = {'lat': float(lat), 'lng': float(lng)}
    except Exception as e:
        st.warning(f"Lỗi đọc station_info.json: {e}")

    # 2. Đọc Excel listLongLat.xlsx làm dự phòng (Ưu tiên 2)
    try:
        if os.path.exists("listLongLat.xlsx"):
            df_coords = pd.read_excel("listLongLat.xlsx")
            # Chuẩn hóa tên cột để tránh lỗi viết hoa/thường
            col_map = {str(col).strip().lower(): col for col in df_coords.columns}
            st_col = col_map.get("station code")
            lat_col = col_map.get("lat")
            long_col = col_map.get("long")
            
            if st_col and lat_col and long_col:
                df_clean = df_coords.dropna(subset=[st_col, lat_col, long_col])
                for _, row in df_clean.iterrows():
                    core_code = extract_core_station_code(row[st_col])
                    # Chỉ cập nhật nếu trạm chưa có tọa độ từ file JSON
                    if core_code and core_code not in coords_map:
                        coords_map[core_code] = {'lat': float(row[lat_col]), 'lng': float(row[long_col])}
    except Exception as e:
        st.warning(f"Lỗi đọc listLongLat.xlsx: {e}")

    # 3. Xây dựng dictionary Kỹ thuật viên & Region từ list_Stations.json
    try:
        with open("list_Stations.json", 'r', encoding='utf-8') as f:
            list_stations = json.load(f)
            for region, engs in list_stations.items():
                for eng, stations in engs.items():
                    for st in stations:
                        core_code = extract_core_station_code(st)
                        tech_map[core_code] = eng
                        region_map[core_code] = region
    except Exception as e:
        st.warning(f"Lỗi đọc list_Stations.json: {e}")

    return coords_map, tech_map, region_map
def render_map():
    st.title("📍 BẢN ĐỒ GIÁM SÁT SỰ CỐ TRẠM SẠC")
    
    # 1. Nạp dữ liệu
    with st.spinner("Đang đồng bộ dữ liệu tĩnh..."):
        coords_map, tech_map, region_map = load_static_data() # Lấy thêm region_map
        
    with st.spinner("Đang kết nối hệ thống CCTS lấy ticket..."):
        df_tickets = fetch_live_tickets()

    if df_tickets.empty:
        st.info("Hiện không có ticket sự cố nào đang mở.")
        return
    
    # 2. Xử lý logic Map
    m = folium.Map(location=[12.25, 108.5], zoom_start=6) 
    
    total_tickets = len(df_tickets)
    
    col1, col2 = st.columns(2)
    col1.metric("Tổng số sự cố đang mở", total_tickets)
    col2.info("Dữ liệu được làm mới tự động mỗi 5 phút để tối ưu hiệu năng API.")

    missing_stations = [] # Danh sách trạm thiếu tọa độ
    # 3. Gắn Markers (Gom nhóm theo trạm)
    grouped = df_tickets.groupby('Station Code')
    
    for station_code, group in grouped:
        core_code = extract_core_station_code(station_code)
        
        # Kiểm tra KV quản lý
        region = region_map.get(core_code, "Unknown")
        if region == "KV không quản lý":
            continue
            
        # Kiểm tra tọa độ
        if core_code in coords_map:
            lat = coords_map[core_code]['lat']
            lng = coords_map[core_code]['lng']
            tech_name = tech_map.get(core_code, "Unassigned")
            
            # Tính thời gian lâu nhất trong trạm để đặt màu Marker (cho nổi bật)
            # Hoặc bạn có thể quy định: Nếu trạm có BSS thì Marker màu khác
            max_duration = group['Ticket Duration'].apply(parse_duration_to_hours).max()
            color = "darkred" if max_duration > 48 else ("orange" if max_duration >= 24 else "green")
            
            # Tạo popup danh sách
            popup_html = create_station_popup_html(station_code, group, tech_name)
            
            folium.Marker(
                location=[lat, lng],
                # height=300 là chiều cao cố định, nếu nội dung dài hơn, nó sẽ chỉ hiện 1 thanh cuộn này
                popup=folium.Popup(folium.IFrame(html=popup_html, width=300, height=300), max_width=350),
                icon=folium.Icon(color=color, icon="info-sign")
            ).add_to(m)
        else:
            # Vẫn thu thập các trạm thiếu tọa độ như cũ
            # (Lưu ý: Chỉ cần append đại diện 1 dòng của group là đủ)
            first_row = group.iloc[0]
            missing_stations.append({
                "Station Code": station_code,
                "Ticket ID": "Multiple",
                "Problem": "Trạm có nhiều ticket lỗi"
            })
    # 4. Render Bản đồ lên Streamlit
    st_folium(m, width="100%", height=650, returned_objects=[])

    # 5. Hiển thị nút tải file trạm thiếu (nếu có)
    if missing_stations:
        st.divider()
        st.warning(f"⚠️ Có {len(set([x['Station Code'] for x in missing_stations]))} trạm chưa có tọa độ!")
        
        df_missing = pd.DataFrame(missing_stations).drop_duplicates(subset=["Station Code"])
        
        # Chuyển đổi DataFrame thành file Excel trong bộ nhớ
        import io
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
            df_missing.to_excel(writer, index=False, sheet_name='Missing_Stations')
        
        st.download_button(
            label="📥 Tải file danh sách trạm thiếu tọa độ (.xlsx)",
            data=buffer.getvalue(),
            file_name="missing_stations.xlsx",
            mime="application/vnd.ms-excel"
        )
def main():
    # Kiểm tra trạng thái đăng nhập
    if not st.session_state.get("authentication_status"):
        authenticator.login()
        if st.session_state["authentication_status"] is False:
            st.error('Tên đăng nhập hoặc mật khẩu không chính xác')
        elif st.session_state["authentication_status"] is None:
            st.warning('Vui lòng nhập thông tin để tiếp tục.')
        return # Dừng lại nếu chưa đăng nhập

    # --- ĐĂNG NHẬP THÀNH CÔNG ---
    
    # Tạo Header tùy chỉnh để tiết kiệm diện tích
    col1, col2 = st.columns([6, 1])
    col1.subheader("📍 BẢN ĐỒ GIÁM SÁT SỰ CỐ TRẠM SẠC")
    
    # Hiển thị tên (Fixed format) và nút đăng xuất
    with col2:
        # Sử dụng text đơn giản để không bị lỗi form (bỏ bolding nếu cần)
        st.caption(f"👤 {st.session_state['name']}") 
        if st.button("Đăng xuất"):
            authenticator.logout()
            st.rerun()
    st.markdown("""
        <style>
            [data-testid="stSidebar"] {display: none;}
            .block-container {padding-top: 1rem; padding-bottom: 1rem;}
        </style>
    """, unsafe_allow_html=True)
    # Gọi hàm render bản đồ (đã full màn hình nhờ layout="wide")
    render_map()
if __name__ == "__main__":
    main()