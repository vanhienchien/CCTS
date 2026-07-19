import os
import mimetypes
import json
import requests
import time
import pandas as pd  
import warnings
from datetime import datetime, timedelta  # <--- Thêm datetime để xử lý lệch múi giờ

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

from playwright.sync_api import sync_playwright

class CCTSClient:
    def __init__(self, username="esmanager", password="Ccts123.", base_url="https://cloud.cnpowercore.com:8091"):
        self.username = username
        self.password = password
        self.base_url = base_url
        self.session = requests.Session()
        self.token = None
        self.ssoticket = None
        self.base_headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'en-US',
            'content-type': 'application/json;charset=UTF-8',
            'origin': 'https://console.cnpowercore.com',
            'priority': 'u=1, i',
            'referer': 'https://console.cnpowercore.com/',
            'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
        }

    def login(self):
        """Dùng Playwright headless đăng nhập bắt Token y hệt auto_ccts.py"""
        print("[+] Đang khởi động Playwright để đăng nhập...")
        
        token_found = False
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()

            def handle_request_interception(route):
                nonlocal token_found
                request = route.request
                
                if "findCCTSTicket" in request.url and request.method == "POST":
                    try:
                        post_data = request.post_data
                        if post_data:
                            payload = json.loads(post_data)
                            if "token" in payload:
                                self.token = payload["token"]
                                token_found = True
                    except Exception:
                        pass
                
                route.continue_()

            page.route("**/findCCTSTicket**", handle_request_interception)

            try:
                page.goto("https://console.cnpowercore.com/", timeout=90000)
                page.wait_for_load_state("domcontentloaded")
                
                page.fill("input[placeholder*='username or email']", self.username)
                page.fill("input[placeholder*='Password']", self.password)
                page.wait_for_timeout(1000)
                page.click("button:has-text('Log in'), button[type='submit']")
                
                for _ in range(20):
                    if token_found:
                        break
                    page.wait_for_timeout(500)
                
                cookies = context.cookies()
                for c in cookies:
                    if c['name'] == 'ssoticket':
                        self.ssoticket = c['value']

            except Exception as e:
                print(f"[-] Lỗi Playwright: {e}")
            finally:
                browser.close()

        if not self.token or not self.ssoticket:
            raise Exception("[-] Đăng nhập thất bại, không lấy được Token hoặc Cookie ssoticket.")

        self.session.headers.update(self.base_headers)
        self.session.cookies.set('ssoticket', self.ssoticket, domain='cloud.cnpowercore.com')
        print(f"[✓] Đăng nhập thành công! Token: {self.token[:15]}...")

    def _post(self, endpoint, payload):
        """Hàm POST ngầm định tích hợp tự động Token."""
        url = f"{self.base_url}{endpoint}"
        payload['token'] = self.token
        
        res = self.session.post(url, json=payload)
        res_data = res.json()
        
        if res_data.get("code") in ["401", "403", "50001"] or not res_data.get("success", True):
            if "token" in str(res_data.get("message", "")).lower() or res_data.get("code") in ["401", "50001"]:
                print("[!] Token nội bộ đã hết hạn. Đang gọi Playwright Re-login...")
                self.login()
                payload['token'] = self.token
                res = self.session.post(url, json=payload)
                res_data = res.json()
                
        return res_data

    def search_ticket(self, ticket_name_or_id):
        """Tìm kiếm ticket linh hoạt: Ưu tiên tìm theo ID, nếu không được sẽ tìm theo Ticket Name."""
        endpoint = "/ccts/cctsTicket/findCCTSTicket"
        
        if str(ticket_name_or_id).isdigit() and len(str(ticket_name_or_id)) > 10:
             payload_by_id = {
                "page": {"pageNum": 1, "pageSize": 10},
                "cctsTicketId": ticket_name_or_id,
                "timezoneOffset": 420
             }
             res_id = self._post(endpoint, payload_by_id)
             data_id = res_id.get("data", {})
             list_id = data_id.get("list", []) if isinstance(data_id, dict) else data_id
             if not isinstance(list_id, list):
                 list_id = data_id.get("records", [])
                 
             if list_id:
                 return list_id[0]

        payload_by_name = {
            "page": {"pageNum": 1, "pageSize": 10},
            "cctsTicketName": ticket_name_or_id,
            "timezoneOffset": 420
        }
        res_name = self._post(endpoint, payload_by_name)
        
        data_name = res_name.get("data", {})
        list_name = data_name.get("list", []) if isinstance(data_name, dict) else data_name
        if not isinstance(list_name, list):
             list_name = data_name.get("records", [])
             
        if not list_name:
            return None
        return list_name[0]

    def upload_file_to_oss(self, file_path):
        file_name = os.path.basename(file_path)
        ext = file_name.split('.')[-1].lower()
    
        content_type, _ = mimetypes.guess_type(file_path)
        if not content_type:
            content_type = "video/mp4" if ext == "mp4" else "image/jpeg"

        loc_url = f"{self.base_url}/ftp/file/uploadLocation"
        params = {
            "extendName": ext,
            "contentType": content_type,
            "directory": "ccts",
            "token": self.token
        }
    
        res = self.session.get(loc_url, params=params)
        res_json = res.json()
        if not res_json.get("success"):
            raise Exception(f"Không thể lấy uploadLocation: {res_json.get('message')}")

        update_location = res_json["data"]["updateLocation"]
        access_location = res_json["data"]["accessLocation"]

        headers = {
            "content-type": content_type,
            "x-oss-tagging": "ower=powercore"
        }

        with open(file_path, "rb") as f:
            file_data = f.read()

        put_res = requests.put(update_location, headers=headers, data=file_data)

        if put_res.status_code == 200:
            return {
                "cctsTicketSolutionFileName": file_name,
                "cctsTicketSolutionFileLocation": access_location,
                "downloading": False
            }
        else:
            raise Exception(f"OSS Upload lỗi {put_res.status_code}: {put_res.text}")
        
    def add_solution(self, ticket_pk, file_paths, note="KT xuống kiểm tra, trụ không tồn lỗi"):
        """Đẩy các file bằng chứng và ghi chú vào Solution."""
        uploaded_files = []
        for path in file_paths:
            print(f"  -> Uploading: {os.path.basename(path)}")
            file_info = self.upload_file_to_oss(path)
            uploaded_files.append(file_info)

        endpoint = "/ccts/cctsTicketSolution/addCCTSTicketSolution"
        payload = {
            "cctsTicketSolutionType": "Permanent solution",
            "cctsTicketSolutionContent": note,
            "cctsTicketPks": [str(ticket_pk)],
            "cctsTicketSolutionFile": uploaded_files
        }
        return self._post(endpoint, payload)

    def add_solution_simple(self, ticket_pk, type="Permanent solution", note="ok"):
        """Đẩy Solution đơn giản không cần upload ảnh/video."""
        endpoint = "/ccts/cctsTicketSolution/addCCTSTicketSolution"
        payload = {
            "cctsTicketSolutionType": type,
            "cctsTicketSolutionContent": note,
            "cctsTicketPks": [str(ticket_pk)],
            "cctsTicketSolutionFile": []
        }
        return self._post(endpoint, payload)

    def add_additional_info(self, ticket_pk, handling_type="REMOTE", 
                            repair_category="Sửa chữa bảo hành", 
                            handling_code="HANDLING_OTHERS__MIEN_TRU", 
                            error_groups="otd_charger", 
                            error_detail="OTĐ-032", 
                            error_description="", 
                            repair_description="", 
                            operator_staff_comments="", 
                            **kwargs):
        endpoint = "/ccts/cctsTicketVfExt/add"
        
        payload = {
            "ticketPk": str(ticket_pk),
            "handlingType": handling_type,
            "repairCategory": repair_category,
            "handlingCode": handling_code,
            "errorGroups": error_groups,
            "errorDetail": error_detail,
            "errorDescription": error_description,
            "repairDescription": repair_description,
            "operatorStaffComments": operator_staff_comments,
            "isNewRecord": "true"
        }
        
        if kwargs:
            payload.update(kwargs)
            
        return self._post(endpoint, payload)

    def add_event_record_asp(self, ticket_pk, note="ok"):
        """Cập nhật trạng thái Pending for ASP close."""
        endpoint = "/ccts/cctsTicketFollowRecord/addCCTSTicketFollowRecord"
        payload = {
            "followRecordStatus": "Pending for ASP close",
            "followRecordContent": note,
            "cctsTicketPks": [str(ticket_pk)],
            "fileList": []
        }
        return self._post(endpoint, payload)

    def resolve_ticket(self, ticket_pk):
        """Thực hiện Resolve ticket."""
        endpoint = "/ccts/cctsTicket/resolve"
        payload = {
            "cctsTicketPk": str(ticket_pk)
        }
        return self._post(endpoint, payload)

    def close_single_duplicate_ticket(self, dup_ticket_pk, keep_ticket_id, dup_ticket_id=None):
        """Thực hiện quy trình 4 bước đóng ticket trùng hoàn chỉnh."""
        note_text = f"Trùng case {keep_ticket_id}"
        display_id = dup_ticket_id if dup_ticket_id else dup_ticket_pk
        
        print(f"   [API] Đang xử lý đóng Ticket ID: {display_id} (Trùng case: {keep_ticket_id})...")
        
        self.add_solution_simple(dup_ticket_pk, note=note_text)
        self.add_additional_info(
            ticket_pk=dup_ticket_pk,
            handling_type="REMOTE",
            repair_category="Sửa chữa bảo hành",
            handling_code="HANDLING_OTHERS__MIEN_TRU",
            error_groups="otd_charger",
            error_detail="OTĐ-032",
            error_description=note_text,
            repair_description=note_text
        )
        self.add_event_record_asp(dup_ticket_pk, note=note_text)
        res = self.resolve_ticket(dup_ticket_pk)
        
        if res.get("success") or str(res.get("code")) in ["200", "0"]:
            print(f"   ✓ [Thành công] Đã đóng Ticket {display_id}")
            return True
        else:
            print(f"   ✗ [Thất bại] Lỗi Resolve Ticket {display_id}: {res.get('message')}")
            return False
            
    def get_ticket_appointments(self, ticket_pk, token=None, page_num=1, page_size=10):
        """Lấy danh sách lịch hẹn (Appointment List) của một Ticket."""
        active_token = token or getattr(self, 'token', '')
        payload = {
            "ticketPk": str(ticket_pk),
            "page": {"pageNum": page_num, "pageSize": page_size},
            "token": active_token
        }
        return self._post("/ccts/cctsTicketAppointment/list", payload)

    def get_ticket_follow_records(self, ticket_pk, token=None, page_num=1, page_size=10):
        """Lấy danh sách lịch sử xử lý (Follow Record/Event Record) của một Ticket."""
        active_token = token or getattr(self, 'token', '')
        payload = {
            "cctsTicketPk": str(ticket_pk),
            "page": {"pageNum": page_num, "pageSize": page_size},
            "token": active_token
        }
        return self._post("/ccts/cctsTicketFollowRecord/list", payload)

    def create_export_task(self, start_time, end_time, ticket_status=None, sla_timeout=None, offset=420):
        """
        Bước 1: Gửi yêu cầu xuất danh sách ticket sang file Excel với các bộ lọc.
        ĐÃ CẬP NHẬT: Tự động nhận giờ Việt Nam (chuỗi YYYY-MM-DD HH:MM:SS) 
        và trừ đi 7 tiếng để làm payload khớp với Backend hệ thống (UTC).
        """
        # --- XỬ LÝ LỆCH MÚI GIỜ TỰ ĐỘNG Ở ĐÂY ---
        try:
            start_dt = datetime.strptime(str(start_time).strip(), "%Y-%m-%d %H:%M:%S") - timedelta(hours=7)
            start_time_payload = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            start_time_payload = start_time  # Fallback nếu chuỗi truyền vào không đúng format định dạng

        try:
            end_dt = datetime.strptime(str(end_time).strip(), "%Y-%m-%d %H:%M:%S") - timedelta(hours=7)
            end_time_payload = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            end_time_payload = end_time

        endpoint = "/ocpp/exportTask/addTicket"
        
        request_data = {
            "createStartTime": start_time_payload,
            "createStopTime": end_time_payload
        }
        
        if ticket_status:
            request_data["ticketStatus"] = ticket_status
        if sla_timeout is not None:
            request_data["slaTimeout"] = str(sla_timeout)
            
        request_param = json.dumps(request_data)
        
        payload = {
            "requestParam": request_param,
            "offset": offset
        }
        print(f"[+] Đang gửi yêu cầu xuất dữ liệu (Status: {ticket_status}, Payload UTC Start: {start_time_payload})...")
        return self._post(endpoint, payload)

    def get_export_tasks(self, page_num=1, page_size=10):
        """Bước 2: Lấy danh sách nhiệm vụ xuất dữ liệu (Export Tasks)."""
        endpoint = "/ocpp/exportTask/list"
        payload = {
            "page": {"pageNum": page_num, "pageSize": page_size}
        }
        return self._post(endpoint, payload)

    def download_file(self, url, save_path):
        """Bước 3: Tải file từ đường dẫn trực tuyến và lưu lại cục bộ."""
        print(f"[+] Đang tải file từ: {url}")
        try:
            res = self.session.get(url, stream=True, timeout=120)
            if res.status_code == 200:
                with open(save_path, 'wb') as f:
                    for chunk in res.iter_content(chunk_size=8192):
                        if chunk: f.write(chunk)
                print(f"[✓] Tải file thành công! Đã lưu tại: {os.path.abspath(save_path)}")
                return True
            else:
                print(f"[-] Lỗi tải file. HTTP Status Code: {res.status_code}")
                return False
        except Exception as e:
            print(f"[-] Gặp lỗi khi tải file: {e}")
            return False

    def export_and_download_tickets(self, start_time, end_time, ticket_status=None, sla_timeout=None, offset=420, check_interval=5, timeout=180):
        """
        QUY TRÌNH TỰ ĐỘNG KHÔNG GHI FILE XUỐNG Ổ CỨNG:
        Nhận vào tham số thời gian dạng Giờ Việt Nam chuẩn.
        """
        import io 

        res_export = self.create_export_task(
            start_time, end_time, 
            ticket_status=ticket_status, 
            sla_timeout=sla_timeout, 
            offset=offset
        )
        
        if not res_export.get("success") and str(res_export.get("code")) not in ["200", "0"]:
            print(f"[-] Thất bại khi gửi yêu cầu xuất: {res_export.get('message')}")
            return None
        
        print("[+] Gửi yêu cầu xuất thành công! Bắt đầu kiểm tra hàng đợi hoàn thành...")
        
        start_poll = time.time()
        download_url = None
        current_check_interval = check_interval
        
        while time.time() - start_poll < timeout:
            res_tasks = self.get_export_tasks(page_num=1, page_size=5)
            data = res_tasks.get("data", {})
            tasks = data.get("list", []) if isinstance(data, dict) else []
            if not isinstance(tasks, list): tasks = data.get("records", [])
                
            if not tasks:
                time.sleep(current_check_interval)
                continue
            
            latest_task = tasks[0]
            download_url = (latest_task.get("fileUrl") or 
                            latest_task.get("downloadUrl") or 
                            latest_task.get("fileLocation") or 
                            latest_task.get("accessLocation"))
            
            status = str(latest_task.get("status"))
            
            if status == "2" and download_url:
                print(f"[✓] File Excel đã sẵn sàng trên Server! Link: {download_url}")
                break
            
            if latest_task.get("errorMsg"):
                print(f"[-] Tác vụ xuất file bị lỗi từ server: {latest_task.get('errorMsg')}")
                return None
                
            print(f"[*] File chưa sẵn sàng (Trạng thái tác vụ: {status}). Đợi {current_check_interval} giây...")
            time.sleep(current_check_interval)
            current_check_interval = min(current_check_interval + 5, 20) 
            
        if not download_url:
            print("[-] Lỗi: Quá thời gian chờ (Timeout) nhưng file Excel chưa được tạo xong.")
            return None

        print(f"[+] Đang tải dữ liệu Excel trực tiếp vào RAM...")
        try:
            res_file = self.session.get(download_url, timeout=120)
            if res_file.status_code != 200:
                print(f"[-] Lỗi tải dữ liệu. HTTP Status Code: {res_file.status_code}")
                return None
            
            print("[+] Đang phân tích các sheet bằng Pandas từ bộ nhớ tạm...")
            dfs = pd.read_excel(io.BytesIO(res_file.content), sheet_name=None)
            
            required_sheets = ["Ticket Information", "Appointment", "Events Record", "Solutions", "Spare Parts Record", "Additional information"]
            for sheet in required_sheets:
                if sheet not in dfs: dfs[sheet] = pd.DataFrame()
            
            print("[✓] Đọc dữ liệu Excel từ RAM thành công!")
            return dfs
            
        except Exception as e:
            print(f"[-] Gặp lỗi khi xử lý dữ liệu Excel trong bộ nhớ RAM: {e}")
            return None

    def download_export_to_file(self, start_time, end_time, save_path, ticket_status=None, sla_timeout=None, offset=420, check_interval=5, timeout=180):
        """Tải file Excel trực tiếp từ server xuống ổ cứng. Không sử dụng Pandas."""
        res_export = self.create_export_task(start_time, end_time, ticket_status, sla_timeout, offset)
        
        if not res_export.get("success") and str(res_export.get("code")) not in ["200", "0"]:
            print(f"[-] Thất bại khi gửi yêu cầu xuất cho {self.username}: {res_export.get('message')}")
            return False
        
        start_poll = time.time()
        download_url = None
        
        while time.time() - start_poll < timeout:
            res_tasks = self.get_export_tasks(page_num=1, page_size=5)
            data = res_tasks.get("data", {})
            tasks = data.get("list", []) if isinstance(data, dict) else []
            if not isinstance(tasks, list): tasks = data.get("records", [])
                
            if tasks:
                latest_task = tasks[0]
                download_url = (latest_task.get("fileUrl") or latest_task.get("downloadUrl") or 
                                latest_task.get("fileLocation") or latest_task.get("accessLocation"))
                status = str(latest_task.get("status"))
                
                if status == "2" and download_url:
                    print(f"[✓] File đã sẵn sàng cho {self.username}!")
                    break
            
            time.sleep(check_interval)
            
        if not download_url:
            print(f"[-] Quá thời gian chờ tải file cho {self.username}.")
            return False

        print(f"[+] Đang ghi file xuống: {save_path}")
        try:
            res_file = self.session.get(download_url, timeout=120)
            if res_file.status_code == 200:
                with open(save_path, 'wb') as f: f.write(res_file.content)
                return True
            else:
                print(f"[-] Lỗi tải file HTTP {res_file.status_code}")
                return False
        except Exception as e:
            print(f"[-] Lỗi ghi file: {e}")
            return False