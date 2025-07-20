print("开始执行app.py...")

# 导入必要模块
try:
    from flask import Flask, render_template, request, jsonify
    print("已导入flask模块")
except ImportError as e:
    print(f"导入flask失败: {e}")
    exit(1)

try:
    from flask_socketio import SocketIO, emit, disconnect
    print("已导入flask_socketio模块")
except ImportError as e:
    print(f"导入flask_socketio失败: {e}")
    exit(1)

try:
    import mss
    import mss.tools
    print("已导入mss模块")
except ImportError as e:
    print(f"导入mss失败: {e}")
    exit(1)

try:
    from PIL import Image
    print("已导入PIL.Image模块")
except ImportError as e:
    print(f"导入PIL.Image失败: {e}")
    exit(1)

import base64
import io
import threading
import time
import subprocess
import os
import logging
import configparser
from logging.handlers import RotatingFileHandler

print("所有模块导入完成")

# 配置日志
if not os.path.exists('logs'):
    os.makedirs('logs')
file_handler = RotatingFileHandler('server.log', maxBytes=10240, backupCount=10, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'))
file_handler.setLevel(logging.INFO)

app = Flask(__name__)
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)
app.logger.info('服务器应用初始化')

# 读取配置文件
config = configparser.ConfigParser()
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

if not os.path.exists(config_path):
    app.logger.error(f'配置文件不存在: {config_path}')
    raise FileNotFoundError(f'配置文件不存在: {config_path}')

try:
    config.read(config_path, encoding='utf-8')
    app.logger.info(f'成功读取配置文件: {config_path}')
    
    # 安全配置
    AUTH_KEY = config.get('Security', 'auth_key')
    
    # 服务器配置
    app.config['SECRET_KEY'] = config.get('Server', 'secret_key', fallback='secret!')
    SERVER_PORT = config.getint('Server', 'port', fallback=5000)
    SERVER_HOST = config.get('Server', 'host', fallback='0.0.0.0')
    DEBUG_MODE = config.getboolean('Server', 'debug', fallback=False)
    
    # 捕获配置
    CAPTURE_QUALITY = config.getint('Capture', 'capture_quality', fallback=30)
    CAPTURE_INTERVAL = config.getfloat('Capture', 'capture_interval', fallback=0.1)
    MONITOR_INDEX = config.getint('Capture', 'monitor_index', fallback=1)
    
    # 日志配置
    LOG_LEVEL = config.get('Logging', 'log_level', fallback='INFO').upper()
    LOG_MAX_SIZE = config.getint('Logging', 'log_max_size', fallback=10)
    LOG_BACKUP_COUNT = config.getint('Logging', 'log_backup_count', fallback=5)
    
except Exception as e:
    app.logger.error(f'读取配置文件失败: {str(e)}', exc_info=True)
    raise

# 启用模板自动重载
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 初始化SocketIO
print("初始化SocketIO...")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', logger=True, engineio_logger=True)

# 存储已认证的客户端（使用线程锁保证安全）
authenticated_clients = set()
# 存储客户端暂停状态 (sid: is_paused)
pause_status = {}
clients_lock = threading.Lock()

# 屏幕捕获状态
capture_running = False

# 请求日志中间件
@app.before_request
def log_request():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    app.logger.info(f"[{current_time}] 收到请求 - 方法: {request.method}, 路径: {request.path}, IP: {request.remote_addr}")
    print(f"[{current_time}] 收到请求 - 方法: {request.method}, 路径: {request.path}, IP: {request.remote_addr}")

# SocketIO连接处理
@socketio.on('connect')
def handle_connect():
    try:
        current_time = time.strftime('%Y-%m-%d %H:%M:%S')
        client_sid = request.sid
        client_ip = request.remote_addr
        app.logger.info(f"[{current_time}] 收到客户端连接请求 - SID: {client_sid}, IP: {client_ip}")
        print(f"[{current_time}] 收到客户端连接请求 - SID: {client_sid}, IP: {client_ip}")
        
        # 初始化暂停状态
        with clients_lock:
            pause_status[client_sid] = False
        
        # 发送连接确认
        emit('connection_ack', {
            'status': 'connected', 
            'message': '服务器已接收连接请求', 
            'sid': client_sid, 
            'time': current_time
        })
    except Exception as e:
        app.logger.error(f"连接处理错误: {str(e)}", exc_info=True)
        print(f"连接处理错误: {str(e)}")

# 认证处理
@socketio.on('authenticate')
def handle_authenticate(client_key):
    global capture_running
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    client_sid = request.sid
    app.logger.info(f"[{current_time}] 收到认证请求 - SID: {client_sid}")
    
    # 从配置文件读取的密钥验证
    if client_key == AUTH_KEY:
        with clients_lock:
            authenticated_clients.add(client_sid)
            pause_status[client_sid] = False  # 认证成功后默认不暂停
        
        app.logger.info(f"[{current_time}] 认证成功 - SID: {client_sid}")
        emit('authenticated', {
            'status': 'success', 
            'message': '认证成功', 
            'time': current_time
        })
        
        # 启动屏幕捕获（如果尚未启动）
        if not capture_running:
            app.logger.info(f"[{current_time}] 启动屏幕捕获循环")
            capture_running = True
            socketio.start_background_task(screen_capture_loop)
    else:
            app.logger.warning(f"[{current_time}] 认证失败 - SID: {client_sid}")
            emit('authentication_failed', {
                'status': 'error', 
                'message': '密码错误，请重新输入', 
                'time': current_time
            })
            disconnect()

# 处理客户端暂停/继续请求
@socketio.on('toggle_pause')
def handle_toggle_pause():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    client_sid = request.sid
    
    # 验证客户端是否已认证
    with clients_lock:
        if client_sid not in authenticated_clients:
            app.logger.warning(f"[{current_time}] 未认证客户端尝试控制暂停 - SID: {client_sid}")
            emit('pause_response', {
                'status': 'error',
                'message': '未授权访问，请先认证'
            })
            return
        
        # 切换暂停状态
        pause_status[client_sid] = not pause_status[client_sid]
        new_status = pause_status[client_sid]
    
    status_text = "已暂停" if new_status else "已继续"
    app.logger.info(f"[{current_time}] 客户端{status_text}数据传输 - SID: {client_sid}")
    emit('pause_response', {
        'status': 'success',
        'message': f'画面{status_text}',
        'is_paused': new_status,
        'time': current_time
    })

# 屏幕捕获循环（修改核心：只向未暂停的客户端发送数据）
def screen_capture_loop():
    global capture_running
    app.logger.info("屏幕捕获循环已启动")
    
    try:
        with mss.mss() as sct:
            # 选择合适的显示器
            if len(sct.monitors) >= 2:
                monitor = sct.monitors[1]  # 主显示器
                app.logger.info(f"使用主显示器: {monitor}")
            else:
                monitor = sct.monitors[0]  # 虚拟全屏
                app.logger.warning(f"使用虚拟全屏: {monitor}")

            while capture_running:
                # 检查是否有已认证的客户端
                with clients_lock:
                    has_active_clients = any(
                        not pause_status[sid]  # 只算未暂停的客户端
                        for sid in authenticated_clients
                    )

                if has_active_clients:
                    try:
                        # 捕获屏幕
                        screenshot = sct.grab(monitor)
                        
                        # 转换为PIL图像
                        img = Image.frombytes(
                            'RGB', 
                            screenshot.size, 
                            screenshot.bgra, 
                            'raw', 
                            'BGRX'
                        )
                        
                        # 压缩图像
                        buffered = io.BytesIO()
                        img.save(buffered, format='JPEG', quality=30)  # 质量可根据需求调整
                        buffered.seek(0)
                        
                        # 转换为base64
                        img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                        
                        # 只向未暂停的客户端发送数据
                        with clients_lock:
                            # 复制当前状态快照，避免迭代中修改集合
                            active_clients = [
                                sid for sid in authenticated_clients
                                if not pause_status.get(sid, True)  # 默认为True（暂停）
                            ]
                        
                        # 向所有活跃客户端发送数据
                        for client_sid in active_clients:
                            socketio.emit('screen_update', {
                                'image_data': img_base64
                            }, room=client_sid)
                        
                    except Exception as e:
                        app.logger.error(f"屏幕捕获/处理错误: {str(e)}", exc_info=True)
                        time.sleep(1)  # 出错时暂停一下
                else:
                    # 没有活跃客户端时暂停循环（降低CPU占用）
                    app.logger.info("没有活跃客户端（所有已认证客户端均暂停），暂停屏幕捕获")
                    time.sleep(2)
                
                # 控制帧率（约10帧/秒）
                time.sleep(0.1)
            
            app.logger.info("屏幕捕获循环正常结束")
    
    except Exception as e:
        app.logger.error(f"屏幕捕获循环异常: {str(e)}", exc_info=True)
    finally:
        capture_running = False

# 断开连接处理
@socketio.on('disconnect')
def handle_disconnect():
    global capture_running
    client_sid = request.sid
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    
    with clients_lock:
        # 清理客户端状态
        if client_sid in authenticated_clients:
            authenticated_clients.remove(client_sid)
            app.logger.info(f"[{current_time}] 客户端断开连接 - SID: {client_sid}")
        
        if client_sid in pause_status:
            del pause_status[client_sid]
        
        # 检查是否还有活跃客户端（已认证且未暂停）
        has_active_clients = any(
            not pause_status.get(sid, True)
            for sid in authenticated_clients
        )
        
        # 如果没有活跃客户端，停止捕获
        if not has_active_clients:
            app.logger.info(f"[{current_time}] 所有客户端均已断开或暂停，停止屏幕捕获")
            capture_running = False

# 服务器命令处理
@socketio.on('server_command')
def handle_server_command(data):
    client_sid = request.sid
    
    # 检查权限
    with clients_lock:
        if client_sid not in authenticated_clients:
            emit('command_response', {
                'status': 'error', 
                'message': '未授权访问'
            })
            return
    
    action = data.get('action')
    command = None
    
    if action == 'shutdown':
        command = ['shutdown', '/s', '/t', '0']
    elif action == 'restart':
        command = ['shutdown', '/r', '/t', '0']
    elif action == 'logout':
        command = ['shutdown', '/l']
    else:
        emit('command_response', {
            'status': 'error', 
            'message': '无效命令'
        })
        return
    
    try:
        subprocess.run(command, check=True)
        emit('command_response', {
            'status': 'success', 
            'message': f'命令已执行: {action}'
        })
    except subprocess.CalledProcessError as e:
        emit('command_response', {
            'status': 'error', 
            'message': f'命令执行失败: {str(e)}'
        })

# 路由定义 
@app.route('/ping')
def ping():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    client_ip = request.remote_addr
    app.logger.info(f"[{current_time}] 收到Ping请求 - IP: {client_ip}")
    return 'OK', 200

@app.route('/test')
def test_endpoint():
    return 'TEST OK', 200

@app.route('/', methods=['GET', 'POST'])
def index():
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    client_ip = request.remote_addr
    app.logger.info(f'[{current_time}] 收到页面请求: /, 客户端IP: {client_ip}')
    
    if request.method == 'POST':
        key = request.form.get('key')
        if key == AUTH_KEY:
            return render_template('viewer.html')
        else:
            return render_template('index.html', error='密钥错误')
    
    return render_template('index.html')

# 打印所有注册路由
app.logger.info('已注册路由:')
for rule in app.url_map.iter_rules():
    app.logger.info(f'  {rule}')

# 修改服务器启动部分
if __name__ == '__main__':
    print("开始启动服务器...")
    try:
        print("尝试启动SocketIO服务器...")
        # 使用配置的端口和主机
        socketio.run(app, host=SERVER_HOST, port=SERVER_PORT, debug=DEBUG_MODE, log_output=True)
    except Exception as e:
        print(f"服务器启动失败: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # 记录错误日志
        with open('server_error.log', 'a') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - 服务器启动失败: {str(e)}\n")
            traceback.print_exc(file=f)
