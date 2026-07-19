import streamlit as st
import folium
from streamlit_folium import st_folium
import json
import pandas as pd
import re
import os
import subprocess
from datetime import datetime, timezone
from api_client import CCTSClient
from utils import extract_core_station_code, parse_duration_to_hours

st.set_page_config(layout="wide", page_title="CCTS Map")

def install_playwright():
    if not os.path.exists("/home/appuser/.cache/ms-playwright"):
        print("Đang tải trình duyệt Playwright, vui lòng đợi trong vài giây...")
        subprocess.run(["playwright", "install", "chromium"])
        print("Tải xong!")

# Gọi hàm này trước khi bắt đầu logic chính của app
install_playwright()

# ==========================================
# ⚙️ 1. CẤU HÌNH TRANG & BẢO MẬT
# ==========================================

# Lấy thông tin đăng nhập từ Streamlit Secrets (thiết lập trên Streamlit Cloud)
try:
    CCTS_USER = st.secrets["CCTS_USERNAME"]
    CCTS_PASS = st.secrets["CCTS_PASSWORD"]
except KeyError:
    st.error("⚠️ Chưa cấu hình thông tin đăng nhập trong Streamlit Secrets!")
    st.stop()

# ==========================================
# 🛠️ 2. MODULE XỬ LÝ DỮ LIỆU TĨNH
# ==========================================
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

# ==========================================
# 🔄 3. MODULE FETCH API (CÓ CACHE)
# ==========================================
@st.cache_data(ttl=600) # Làm mới dữ liệu tự động sau mỗi 5 phút
def fetch_live_tickets():
    """
    Khởi tạo CCTSClient, tự động đăng nhập qua Playwright và gọi trực tiếp endpoint JSON 
    để trích xuất các trường thông tin cần thiết phục vụ bản đồ Live Ticket.
    """
    # Sử dụng trực tiếp biến Global đã lấy từ st.secrets ở đầu file
    client = CCTSClient(username=CCTS_USER, password=CCTS_PASS)
    
    # Thực hiện login lượt đầu tiên để lấy Token và Cookie ssoticket ban đầu
    try:
        client.login()
    except Exception as e:
        st.error(f"Khởi động phiên đăng nhập CCTS thất bại: {e}")
        return pd.DataFrame()

    # Endpoint tìm kiếm/truy vấn danh sách ticket trực tiếp
    endpoint = "/ccts/cctsTicket/findCCTSTicket"
    
    # Payload bộ lọc tương tự như trích xuất trực tuyến
    payload = {
        "page": {"pageNum": 1, "pageSize": 2000},  # Điều chỉnh kích thước trang nếu lượng ticket lớn
        "timezoneOffset": 420,
        "createStartTime": "2026-04-30 17:00:00",
        "createStopTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ticketStatus": ["Open", "Appointment", "Pending for ASP close", "Pending for spare parts"]
    }

    try:
        # Gọi API qua hàm _post nội bộ. Hàm này tự bắt mã 401/403/50001 để tự động 
        # gọi lại Playwright gia hạn token nếu hết hạn giữa chừng.
        res_data = client._post(endpoint, payload)
        
        # Bóc tách danh sách từ cấu trúc JSON phản hồi
        data = res_data.get("data", {})
        tickets = data.get("list", []) if isinstance(data, dict) else data
        if not isinstance(tickets, list):
            tickets = data.get("records", [])

        if not tickets:
            return pd.DataFrame()

        processed_data = []

        # Vòng lặp lấy đúng các thông tin cần thiết từ đối tượng JSON
        for item in tickets:
            # Ánh xạ trực tiếp từ cấu trúc JSON sang định dạng cột mong muốn
            processed_data.append({
                "Ticket ID": item.get("cctsTicketId"),
                "Charge Point ID": item.get("chargeBoxId"),
                "Station Code": item.get("stationCode"),
                "Problem Description": item.get("errorDesc"),
                "Ticket Status": item.get("cctsTicketStatus"),
                "Ticket Duration": item.get("duration")
            })

        df = pd.DataFrame(processed_data)
        
        # Lọc bỏ nhiễu từ các trạm thử nghiệm BSS.No2 giống logic cũ
        if "Problem Description" in df.columns:
            df = df[~df["Problem Description"].astype(str).str.strip().str.startswith("BSS.No2")].copy()
            
        return df

    except Exception as e:
        st.error(f"Gặp lỗi khi xử lý luồng dữ liệu API: {e}")
        return pd.DataFrame()

# ==========================================
# 🎨 4. MODULE RENDER BẢN ĐỒ
# ==========================================
def create_station_popup_html(station_code, tickets_df, tech_name):
    """
    Popup hiển thị các Charge Point trong trạm.
    Mỗi Charge Point sẽ được tô màu theo thời gian tồn tại ticket.
    """

    html_content = f"""
    <div style="font-family:Arial;font-size:12px;min-width:280px;padding:5px;">
        <h4 style="margin:0;color:#1f77b4;">
            Trạm: {station_code}
        </h4>

        <div style="margin-top:4px;margin-bottom:6px;">
            <b>Kỹ thuật viên:</b>
            <span style="color:#2ca02c;font-weight:bold;">
                {tech_name}
            </span>
        </div>

        <hr style="margin:5px 0;">
    """

    # Sắp xếp ticket lâu nhất lên đầu
    tickets_df = tickets_df.copy()
    tickets_df["Hours"] = tickets_df["Ticket Duration"].apply(parse_duration_to_hours)
    tickets_df = tickets_df.sort_values("Hours", ascending=False)

    for _, row in tickets_df.iterrows():

        hours = row["Hours"]

        # =====================
        # Chọn màu theo thời gian
        # =====================

        if hours >= 48:
            card_bg = "#ffe5e5"
            border = "#d62728"
            time_color = "#b30000"

        elif hours >= 24:
            card_bg = "#fff2d9"
            border = "#ff9800"
            time_color = "#c77700"

        else:
            card_bg = "#eaf8ea"
            border = "#2ca02c"
            time_color = "#2ca02c"

        cp_id = str(row["Charge Point ID"])

        icon = "🔋" if cp_id.startswith("BSS") else "⚡"

        html_content += f"""
        <div style="
            background:{card_bg};
            border-left:5px solid {border};
            padding:7px;
            margin-bottom:7px;
            border-radius:5px;
        ">

            <div style="
                color:{border};
                font-weight:bold;
                font-size:12px;
            ">
                {icon} {cp_id}
            </div>

            <div style="margin-top:3px;">
                <b>ID:</b> {row["Ticket ID"]}

                &nbsp;&nbsp;

                <b>TT:</b> {row["Ticket Status"]}
            </div>

            <div>
                <b>Thời gian:</b>

                <span style="
                    color:{time_color};
                    font-weight:bold;
                ">
                    {row["Ticket Duration"]}
                </span>
            </div>

            <div style="
                color:#555;
                margin-top:3px;
                font-style:italic;
            ">
                {row["Problem Description"]}
            </div>

        </div>
        """

    html_content += "</div>"

    return html_content
def create_station_marker(cp_count, color):
    """
    Tạo marker hình giọt nước có hiển thị số Charge Point lỗi.
    """

    color_map = {
        "green": "#6ECC39",
        "orange": "#F5A623",
        "darkred": "#B52B31"
    }

    marker_color = color_map.get(color, "#007bff")

    html = f"""
    <div style="
        position:relative;
        width:40px;
        height:54px;
    ">

        <!-- Pin -->
        <div style="
            position:absolute;
            width:40px;
            height:40px;

            background:{marker_color};

            border-radius:50% 50% 50% 0;

            transform:rotate(-45deg);

            border:3px solid white;

            box-shadow:0 3px 8px rgba(0,0,0,.45);

        "></div>

        <!-- Number -->
        <div style="
            position:absolute;

            width:40px;
            height:40px;

            display:flex;
            align-items:center;
            justify-content:center;

            color:white;
            font-size:16px;
            font-weight:bold;

            z-index:999;

        ">
            {cp_count}
        </div>

    </div>
    """

    return folium.DivIcon(
        html=html,
        icon_size=(40,54),
        icon_anchor=(20,54)
    )
def render_map():
    
    # 1. Nạp dữ liệu
    with st.spinner("Đang đồng bộ dữ liệu tĩnh..."):
        coords_map, tech_map, region_map = load_static_data() 
        
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
            
            # Dùng lại apply(parse_duration_to_hours) để chuyển chuỗi thành số giờ
            max_duration = group['Ticket Duration'].apply(parse_duration_to_hours).max()
            color = "darkred" if max_duration > 48 else ("orange" if max_duration >= 24 else "green")
            
            # Tạo popup danh sách
            popup_html = create_station_popup_html(station_code, group, tech_name)
            
            # ==========================
            # Marker hiển thị số lượng Charge Point lỗi
            # ==========================

            cp_count = len(group)

            folium.Marker(

                location=[lat,lng],

                popup=folium.Popup(
                    folium.IFrame(
                        html=popup_html,
                        width=330,
                        height=320
                    ),
                    max_width=360
                ),

                icon=create_station_marker(
                    cp_count,
                    color
                )

            ).add_to(m)
        else:
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
    # Nút bấm làm mới
    if st.button("🔄 Cập nhật dữ liệu mới"):
        st.rerun()

    # CSS ẩn sidebar (nếu bạn vẫn muốn dùng)
    st.markdown("""
        <style>
            [data-testid="stSidebar"] {display: none;}
            .block-container {padding-top: 1rem; padding-bottom: 1rem;}
        </style>
    """, unsafe_allow_html=True)
    # Header
    st.subheader("📍 BẢN ĐỒ GIÁM SÁT SỰ CỐ TRẠM SẠC ES")
    # Render bản đồ
    render_map()

if __name__ == "__main__":
    main()