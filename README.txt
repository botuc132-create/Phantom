JINNY CHEAT - RENDER + TELEGRAM BẮT BUỘC THAM GIA NHÓM

CÀI ĐẶT
1. Upload toàn bộ file trong ZIP lên thư mục gốc GitHub repo.
2. Render > New + > Blueprint > chọn repo có render.yaml > Apply.
3. Trong Render > service > Environment, thêm:

BOT_TOKEN = token bot lấy từ @BotFather
WEBAPP_URL = https://TEN-APP.onrender.com/
WEBHOOK_SECRET = chuỗi bí mật tự đặt, chỉ chữ và số, dài 20-50 ký tự
ADMIN_IDS = Telegram user ID admin; nhiều ID cách nhau bằng dấu phẩy
REQUIRED_GROUPS = JSON danh sách nhóm, ví dụ một dòng:

[{"chat_id":"@ten_nhom_1","title":"Nhóm thông báo","url":"https://t.me/ten_nhom_1"},{"chat_id":"-1001234567890","title":"Nhóm cộng đồng","url":"https://t.me/ten_nhom_2"}]

Thay toàn bộ tên nhóm, ID và link bằng nhóm thật của bạn. chat_id có thể là @username công khai hoặc ID dạng -100...
Với nhóm riêng tư, dùng ID -100... và link mời thật.
4. Save Changes để Render deploy lại.
5. Thêm bot vào TỪNG nhóm bắt buộc; nên cấp quyền admin để bot kiểm tra thành viên ổn định.
6. Nhắn /start cho bot để thử.

CÁCH HOẠT ĐỘNG
- /start: bot kiểm tra thành viên các nhóm.
- Chưa đủ: hiện nút tham gia từng nhóm và nút Xác minh; chưa hiện nút mở Mini App.
- Bấm Xác minh: bot kiểm tra lại. Còn thiếu thì báo tên nhóm chưa tham gia.
- Đủ: bot hiện nút MỞ MINI APP.
- ID nằm trong ADMIN_IDS được bỏ qua kiểm tra nhóm.

KIỂM TRA
Mở https://TEN-APP.onrender.com/health
telegram_bot_configured phải là true; required_groups phải bằng số nhóm đã cấu hình.

LƯU Ý QUAN TRỌNG
Đây là kiểm tra trong luồng /start của bot. Trang web Mini App gốc vẫn là trang tĩnh; người biết URL trực tiếp vẫn có thể mở URL ngoài bot. Muốn khóa cả truy cập trực tiếp, cần làm thêm xác thực Telegram initData ở backend và bảo vệ API/dữ liệu Mini App.
Source gốc vẫn giữ logic key/sản phẩm bằng LocalStorage, không phải database dùng chung.
Nếu bot không đọc được thành viên nhóm thì người dùng sẽ không được xác minh.
