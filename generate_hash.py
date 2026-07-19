import streamlit_authenticator as stauth

# Danh sách mật khẩu của kỹ thuật viên
passwords = ['ktv1_pass', 'ktv2_pass'] 

# Tạo hash cho từng mật khẩu trong danh sách
hashed_passwords = [stauth.Hasher().hash(p) for p in passwords]

print("Danh sách mật khẩu đã hash:")
print(hashed_passwords)