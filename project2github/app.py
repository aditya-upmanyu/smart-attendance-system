"""
🏆 SMART ATTENDANCE SYSTEM - PRODUCTION VERSION
Face Recognition Attendance System with Firebase & Gemini AI
Ready for deployment on Render/Railway
"""

import os
import cv2
import numpy as np
try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False
    import logging
    logging.warning("⚠️ face_recognition not available - using fallback mode")

from flask import Flask, render_template, request, redirect, url_for, Response, jsonify, session, send_file
from flask_socketio import SocketIO, emit
import firebase_admin
from firebase_admin import credentials, db, storage
from datetime import datetime, timezone, timedelta
import threading
import time
import logging
from dotenv import load_dotenv
import json
import qrcode
from io import BytesIO
import base64
import csv

# Load environment variables
load_dotenv()

# ==================== CONFIGURATION ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_THIS_IN_PRODUCTION")
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25
)

# ==================== FIREBASE INITIALIZATION ====================
bucket = None
try:
    # Try to get credentials from environment variable (for Render/Railway)
    firebase_creds_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')

    if firebase_creds_json:
        # Production: Load from environment variable
        logger.info("Loading Firebase credentials from environment variable")
        cred_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(cred_dict)
    elif os.path.exists("serviceAccountKey.json"):
        # Development: Load from file
        logger.info("Loading Firebase credentials from serviceAccountKey.json")
        cred = credentials.Certificate("serviceAccountKey.json")
    else:
        # Fallback to Application Default Credentials
        logger.info("Using Application Default Credentials")
        cred = credentials.ApplicationDefault()

    firebase_admin.initialize_app(cred, {
        "databaseURL": "https://smart-attendance-16fbd-default-rtdb.firebaseio.com/",
        "storageBucket": "smart-attendance-16fbd.firebasestorage.app"
    })
    bucket = storage.bucket()
    logger.info("✅ Firebase initialized successfully")
except Exception as e:
    logger.error(f"❌ Firebase initialization failed: {e}")
    bucket = None

# ==================== GEMINI AI INITIALIZATION ====================
gemini_client = None
try:
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY")

    if api_key:
        genai.configure(api_key=api_key)
        gemini_client = genai.GenerativeModel('gemini-2.0-flash-exp')
        logger.info("✅ Gemini AI initialized successfully")
    else:
        logger.warning("⚠️ GEMINI_API_KEY not set - AI features will use fallback")
except Exception as e:
    logger.warning(f"⚠️ Gemini AI unavailable: {e}")
    gemini_client = None

# ==================== ATTENDANCE CACHE ====================
class AttendanceCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.encodings = []
        self.info = []
        self.marked = set()
        self.last_seen = {}
        self.frame_skip = 2


    def load_encodings(self, class_id):
        with self.lock:
            self.encodings.clear()
            self.info.clear()
            self.marked.clear()
            self.last_seen.clear()


            try:
                students = db.reference("students").get()
                if not students:
                    return


                for sid, data in students.items():
                    if data.get("class_id") == class_id:
                        encoding = np.array(data.get("encoding", []))
                        if encoding.size > 0:
                            self.encodings.append(encoding)
                            self.info.append({
                                "id": sid,
                                "name": data.get("name", "Unknown"),
                                "roll_no": data.get("roll_no", "N/A")
                            })
                logger.info(f"✅ Loaded {len(self.encodings)} students for {class_id}")
            except Exception as e:
                logger.error(f"Error loading encodings: {e}")


    def mark_attendance(self, student_id):
        current_time = time.time()
        with self.lock:
            if student_id in self.marked:
                return False
            last_time = self.last_seen.get(student_id, 0)
            if current_time - last_time < 3:
                return False
            self.last_seen[student_id] = current_time
            self.marked.add(student_id)
            return True


cache = AttendanceCache()


# ==================== UTILITY FUNCTIONS ====================
def get_attendance_stats(class_id, date=None):
    """Get attendance statistics for a class"""
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        attendance_data = db.reference(f"attendance/{date}/{class_id}").get()
        class_ref = db.reference(f'classes/{class_id}').get()
        total = len(class_ref.get('students', {})) if class_ref else 0
        present = len(attendance_data) if attendance_data else 0
        return {
            "total": total,
            "present": present,
            "absent": total - present,
            "percentage": round((present / total * 100), 2) if total > 0 else 0
        }
    except:
        return {"total": 0, "present": 0, "absent": 0, "percentage": 0}


def generate_qr_code(student_id):
    """Generate QR code for student"""
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(f"STUDENT:{student_id}")
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return base64.b64encode(buffer.getvalue()).decode()
    except:
        return ""


# ==================== AUTHENTICATION ROUTES ====================
@app.route('/')
def index():
    if 'logged_in' in session:
        if session.get('user_type') == 'teacher':
            return redirect(url_for('dashboard'))
        return redirect(url_for('student_dashboard'))
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    user_type = request.form.get('user_type', 'teacher')


    if user_type == 'teacher':
        admin_user = os.environ.get("ADMIN_USERNAME", "admin")
        admin_pass = os.environ.get("ADMIN_PASSWORD", "gla123")
        if username == admin_user and password == admin_pass:
            session['logged_in'] = True
            session['username'] = username
            session['user_type'] = 'teacher'
            logger.info(f"✅ Teacher {username} logged in")
            return redirect(url_for('dashboard'))
    else:
        try:
            student = db.reference(f'students/{username}').get()
            if student and student.get('password') == password:
                session['logged_in'] = True
                session['username'] = username
                session['user_type'] = 'student'
                session['student_id'] = username
                logger.info(f"✅ Student {username} logged in")
                return redirect(url_for('student_dashboard'))
        except:
            pass


    return render_template('login.html', error="Invalid credentials"), 401


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ==================== TEACHER DASHBOARD ====================
@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))


    try:
        classes_ref = db.reference('classes').get()
        classes = []
        total_students = 0
        total_present = 0


        if classes_ref:
            for class_id, class_data in classes_ref.items():
                student_count = len(class_data.get('students', {}))
                stats = get_attendance_stats(class_id)
                total_students += stats['total']
                total_present += stats['present']


                classes.append({
                    "id": class_id,
                    "name": class_data.get('name', 'Unknown'),
                    "time": class_data.get('time', 'N/A'),
                    "room": class_data.get('room', 'N/A'),
                    "student_count": student_count,
                    "today_present": stats['present'],
                    "today_percentage": stats['percentage']
                })


        overall_percentage = round((total_present / total_students * 100), 2) if total_students > 0 else 0


        return render_template('dashboard.html', 
                             classes=classes,
                             total_students=total_students,
                             total_present=total_present,
                             overall_percentage=overall_percentage,
                             username=session.get('username'))
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return render_template('dashboard.html', classes=[], total_students=0, 
                             total_present=0, overall_percentage=0, error="Error loading classes")


# ==================== STUDENT DASHBOARD ====================
@app.route('/student/dashboard')
def student_dashboard():
    if 'logged_in' not in session or session.get('user_type') != 'student':
        return redirect(url_for('index'))


    try:
        student_id = session.get('student_id')
        student_data = db.reference(f'students/{student_id}').get()


        if not student_data:
            return "Student not found", 404


        attendance_history = []
        today = datetime.now(timezone.utc)
        class_id = student_data.get('class_id')


        present_days = 0
        for i in range(30):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            attendance = db.reference(f"attendance/{date}/{class_id}/{student_id}").get()
            status = "Present" if attendance else "Absent"
            if status == "Present":
                present_days += 1
            attendance_history.append({
                "date": date,
                "status": status,
                "time": attendance.get('time', '-') if attendance else '-'
            })


        attendance_percentage = round((present_days / 30 * 100), 2)
        qr_code = generate_qr_code(student_id)


        return render_template('student_dashboard.html',
                             student=student_data,
                             student_id=student_id,
                             attendance_history=attendance_history,
                             present_days=present_days,
                             total_days=30,
                             attendance_percentage=attendance_percentage,
                             qr_code=qr_code)
    except Exception as e:
        logger.error(f"Student dashboard error: {e}")
        return "Error loading dashboard", 500


# ==================== CLASS MANAGEMENT ====================
@app.route('/create_class', methods=['GET', 'POST'])
def create_class():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))


    if request.method == 'POST':
        try:
            class_id = request.form.get('class_id', '').strip().upper()
            class_name = request.form.get('class_name', '').strip()
            class_time = request.form.get('class_time', '').strip()
            room_number = request.form.get('room_number', '').strip()
            capacity = int(request.form.get('capacity', 50))


            if not all([class_id, class_name]):
                return jsonify({"status": "error", "message": "Missing fields"}), 400


            existing = db.reference(f'classes/{class_id}').get()
            if existing:
                return jsonify({"status": "error", "message": "Class exists"}), 400


            db.reference(f'classes/{class_id}').set({
                "name": class_name,
                "time": class_time,
                "room": room_number,
                "capacity": capacity,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": session.get('username'),
                "students": {}
            })


            logger.info(f"✅ Class {class_id} created")
            return jsonify({"status": "success", "message": f"Class {class_name} created"}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500


    return render_template('create_class.html')


@app.route('/manage_class/<class_id>')
def manage_class(class_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))


    try:
        class_data = db.reference(f'classes/{class_id}').get()
        if not class_data:
            return "Class not found", 404


        students = []
        students_ref = db.reference('students').get()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")


        if students_ref:
            for sid, sdata in students_ref.items():
                if sdata.get('class_id') == class_id:
                    attendance_today = db.reference(f"attendance/{today}/{class_id}/{sid}").get()
                    total_classes = sdata.get('total_classes', 1)
                    attended = sdata.get('attended_classes', 0)
                    students.append({
                        "id": sid,
                        "name": sdata.get('name'),
                        "roll_no": sdata.get('roll_no'),
                        "email": sdata.get('email', 'N/A'),
                        "present_today": "Yes" if attendance_today else "No",
                        "attendance_percentage": round((attended / total_classes * 100), 2) if total_classes > 0 else 0
                    })


        return render_template('manage_class.html', 
                             class_id=class_id,
                             class_data=class_data,
                             students=students)
    except Exception as e:
        logger.error(f"Error: {e}")
        return "Error loading class", 500


# ==================== ADD STUDENT PAGE ====================
@app.route('/add_student_page/<class_id>')
def add_student_page(class_id):
    """Page to add student with form"""
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))


    class_data = db.reference(f'classes/{class_id}').get()
    class_name = class_data.get('name', class_id) if class_data else class_id


    return render_template('add_student.html', class_id=class_id, class_name=class_name)


# ==================== STUDENT MANAGEMENT ====================
@app.route('/add_student', methods=['POST'])
def add_student():
    """Add student with face capture"""
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return jsonify({"status": "error", "message": "Unauthorized"}), 401


    student_id = request.form.get('student_id', '').strip()
    name = request.form.get('name', '').strip()
    class_id = request.form.get('class_id', '').strip()
    roll_no = request.form.get('roll_no', '').strip()
    email = request.form.get('email', '').strip()


    if not all([student_id, name, class_id]):
        return jsonify({"status": "error", "message": "Missing required fields"}), 400


    existing = db.reference(f'students/{student_id}').get()
    if existing:
        return jsonify({"status": "error", "message": "Student ID already exists"}), 400


    # Capture face
    try:
        cam = cv2.VideoCapture(0)
        if not cam.isOpened():
            return jsonify({"status": "error", "message": "Camera not accessible"}), 400


        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)


        # Warm up camera
        for _ in range(10):
            cam.read()


        frames = []
        for _ in range(5):
            success, frame = cam.read()
            if success:
                frames.append(frame)
            time.sleep(0.1)


        cam.release()


        if not frames:
            return jsonify({"status": "error", "message": "Failed to capture frames"}), 400


        all_encodings = []
        best_frame = None


        for frame in frames:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            encodings = face_recognition.face_encodings(rgb_frame)
            if encodings:
                all_encodings.append(encodings[0])
                if best_frame is None:
                    best_frame = frame


        if not all_encodings:
            return jsonify({"status": "error", "message": "No face detected. Please look at camera."}), 400


        avg_encoding = np.mean(all_encodings, axis=0).tolist()


        # Save to Firebase
        db.reference(f'students/{student_id}').set({
            "name": name,
            "class_id": class_id,
            "roll_no": roll_no,
            "email": email,
            "encoding": avg_encoding,
            "password": student_id,
            "last_attendance": "Never",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_classes": 0,
            "attended_classes": 0
        })


        db.reference(f'classes/{class_id}/students/{student_id}').set(True)


        # Upload photo to storage
        if bucket:
            blob = bucket.blob(f"student_faces/{student_id}.jpg")
            _, buffer = cv2.imencode('.jpg', best_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            blob.upload_from_string(buffer.tobytes(), content_type='image/jpeg')


        logger.info(f"✅ Student {name} ({student_id}) added")
        return jsonify({"status": "success", "message": f"Student {name} added! Default password: {student_id}"}), 200


    except Exception as e:
        logger.error(f"Error adding student: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/delete_student/<student_id>', methods=['POST', 'DELETE'])
def delete_student(student_id):
    """Delete a student"""
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return jsonify({"status": "error", "message": "Unauthorized"}), 401


    try:
        student_data = db.reference(f'students/{student_id}').get()
        if not student_data:
            return jsonify({"status": "error", "message": "Student not found"}), 404


        class_id = student_data.get('class_id')


        # Remove from database
        db.reference(f'students/{student_id}').delete()
        db.reference(f'classes/{class_id}/students/{student_id}').delete()


        # Delete photo from storage
        if bucket:
            try:
                blob = bucket.blob(f"student_faces/{student_id}.jpg")
                blob.delete()
            except:
                pass


        logger.info(f"✅ Student {student_id} deleted")
        return jsonify({"status": "success", "message": "Student deleted successfully"}), 200
    except Exception as e:
        logger.error(f"Error deleting student: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ==================== ATTENDANCE SYSTEM ====================
@app.route('/attendance/<class_id>')
def attendance(class_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))


    class_info = db.reference(f'classes/{class_id}').get()
    class_name = class_info.get('name', class_id) if class_info else class_id
    return render_template('attendance.html', class_id=class_id, class_name=class_name)


def gen_frames(class_id):
    """Generate video frames with face recognition"""
    camera = cv2.VideoCapture(0)
    if not camera.isOpened():
        logger.error("Camera not accessible")
        return


    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera.set(cv2.CAP_PROP_FPS, 30)


    cache.load_encodings(class_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frame_count = 0
    last_locations = []
    last_names = []


    while True:
        success, frame = camera.read()
        if not success:
            break


        frame_count += 1
        process_frame = (frame_count % cache.frame_skip == 0)


        if process_frame and cache.encodings:
            small_frame = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
            rgb_small = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(rgb_small, model='hog')
            face_encodings = face_recognition.face_encodings(rgb_small, face_locations)


            last_locations = []
            last_names = []


            for encoding, location in zip(face_encodings, face_locations):
                matches = face_recognition.compare_faces(cache.encodings, encoding, tolerance=0.45)
                face_distances = face_recognition.face_distance(cache.encodings, encoding)


                name = "Unknown"
                sid = None
                confidence = 0


                if len(face_distances) > 0:
                    best_match = np.argmin(face_distances)
                    if matches[best_match]:
                        student = cache.info[best_match]
                        name = student["name"]
                        sid = student["id"]
                        confidence = 1 - face_distances[best_match]


                        if cache.mark_attendance(sid):
                            timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            db.reference(f"attendance/{today}/{class_id}/{sid}").set({
                                "name": name,
                                "time": timestamp,
                                "status": "Present",
                                "confidence": float(confidence),
                                "marked_at": datetime.now(timezone.utc).isoformat()
                            })


                            student_ref = db.reference(f'students/{sid}')
                            current_attended = student_ref.child('attended_classes').get() or 0
                            student_ref.update({
                                "last_attendance": today,
                                "attended_classes": current_attended + 1
                            })


                            socketio.emit('new_attendance', {
                                'name': name,
                                'id': sid,
                                'time': timestamp,
                                'confidence': f"{confidence*100:.1f}%"
                            })


                            logger.info(f"✅ {name} marked present")


                top, right, bottom, left = [v * 4 for v in location]
                last_locations.append((top, right, bottom, left))
                last_names.append((name, confidence))


        for (top, right, bottom, left), (name, confidence) in zip(last_locations, last_names):
            color = (0, 200, 0) if name != "Unknown" else (0, 0, 255)
            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
            cv2.rectangle(frame, (left, bottom - 35), (right, bottom), color, cv2.FILLED)
            label = f"{name}" if name == "Unknown" else f"{name} ({confidence*100:.0f}%)"
            cv2.putText(frame, label, (left + 6, bottom - 6),
                       cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1)


        cv2.putText(frame, f"Students: {len(cache.encodings)} | Marked: {len(cache.marked)}",
                   (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)


        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')


    camera.release()


@app.route('/video_feed/<class_id>')
def video_feed(class_id):
    if 'logged_in' not in session:
        return "Unauthorized", 401
    return Response(gen_frames(class_id),
                   mimetype='multipart/x-mixed-replace; boundary=frame')


# ==================== ANALYTICS & AI ====================
@app.route('/analytics')
def analytics():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))


    # Get list of classes for dropdown
    classes_ref = db.reference('classes').get()
    classes = []
    if classes_ref:
        for class_id, class_data in classes_ref.items():
            classes.append({
                "id": class_id,
                "name": class_data.get('name', class_id)
            })


    return render_template('analytics.html', classes=classes)


@app.route('/api/ai-insights/<class_id>')
def ai_insights(class_id):
    """Get AI insights with fallback"""
    if 'logged_in' not in session:
        return jsonify({"error": "Unauthorized"}), 401


    try:
        stats = get_attendance_stats(class_id)


        if not gemini_client:
            # Fallback insights when Gemini is not available
            insights_text = f"""Based on the data analysis:


1. Room Utilization: Current attendance is {stats['percentage']}% - {'Optimal usage' if 70 <= stats['percentage'] <= 90 else 'Could be improved'}


2. Attendance Pattern: {stats['present']} out of {stats['total']} students present today


3. Recommendation: {'Maintain current engagement strategies' if stats['percentage'] > 75 else 'Consider implementing reminder systems to improve attendance'}"""


            return jsonify({
                "status": "success",
                "insights": insights_text,
                "stats": stats,
                "ai_enabled": False
            })


        # Use Gemini AI
        prompt = f"""Analyze this classroom attendance and provide 3 brief insights:
Class: {class_id}
Total Students: {stats['total']}
Present Today: {stats['present']}
Attendance Rate: {stats['percentage']}%


Provide exactly 3 insights (each under 25 words):
1. Room utilization insight
2. Attendance pattern observation  
3. One specific actionable recommendation"""


        response = gemini_client.generate_content(prompt)


        return jsonify({
            "status": "success",
            "insights": response.text,
            "stats": stats,
            "ai_enabled": True
        })
    except Exception as e:
        logger.error(f"AI insights error: {e}")
        # Fallback response
        stats = get_attendance_stats(class_id)
        return jsonify({
            "status": "success",
            "insights": f"Attendance: {stats['present']}/{stats['total']} ({stats['percentage']}%). Analysis engine temporarily unavailable.",
            "stats": stats,
            "ai_enabled": False
        })


@app.route('/api/room-analytics')
def room_analytics():
    if 'logged_in' not in session:
        return jsonify({"error": "Unauthorized"}), 401


    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        classes_ref = db.reference('classes').get()
        analytics = []


        if classes_ref:
            for class_id, class_data in classes_ref.items():
                stats = get_attendance_stats(class_id)
                utilization = stats['percentage']


                analytics.append({
                    "class_id": class_id,
                    "class_name": class_data.get('name'),
                    "capacity": stats['total'],
                    "present": stats['present'],
                    "utilization": utilization,
                    "status": "optimal" if 70 <= utilization <= 90 else 
                             "underutilized" if utilization < 70 else "overcrowded"
                })


        avg_util = round(sum(r['utilization'] for r in analytics) / len(analytics), 1) if analytics else 0


        return jsonify({
            "date": today,
            "rooms": analytics,
            "summary": {"total_rooms": len(analytics), "avg_utilization": avg_util}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Continue in next part...
"""
🏆 SMART ATTENDANCE SYSTEM - ENHANCED VERSION
Fixed: Add Student Feature, AI Insights, Improved UI
Google Technologies: Firebase, Gemini AI, Cloud Storage
"""


import os
import cv2
import numpy as np
import face_recognition
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify, session, send_file
from flask_socketio import SocketIO, emit
import firebase_admin
from firebase_admin import credentials, db, storage
from datetime import datetime, timezone, timedelta
import threading
import time
import logging
from dotenv import load_dotenv
import json
import qrcode
from io import BytesIO
import base64
import csv


load_dotenv()


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gla_hackathon_2026_secret")
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)


# Firebase initialization
try:
    SERVICE_ACCOUNT_FILE = "serviceAccountKey.json"
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    else:
        cred = credentials.ApplicationDefault()


    firebase_admin.initialize_app(cred, {
        "databaseURL": "https://smart-attendance-16fbd-default-rtdb.firebaseio.com/",
        "storageBucket": "smart-attendance-16fbd.firebasestorage.app"
    })
    bucket = storage.bucket()
    logger.info("✅ Firebase initialized")
except Exception as e:
    logger.error(f"❌ Firebase init failed: {e}")
    bucket = None


# Gemini AI initialization
gemini_client = None
try:
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and api_key != "your_gemini_api_key_here":
        genai.configure(api_key=api_key)
        gemini_client = genai.GenerativeModel('gemini-2.0-flash-exp')
        logger.info("✅ Gemini AI initialized")
    else:
        logger.warning("⚠️ Gemini API key not configured")
except Exception as e:
    logger.warning(f"⚠️ Gemini unavailable: {e}")


class AttendanceCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.encodings = []
        self.info = []
        self.marked = set()
        self.last_seen = {}
        self.frame_skip = 2


    def load_encodings(self, class_id):
        with self.lock:
            self.encodings.clear()
            self.info.clear()
            self.marked.clear()
            self.last_seen.clear()
            try:
                students = db.reference("students").get()
                if students:
                    for sid, data in students.items():
                        if data.get("class_id") == class_id:
                            encoding = np.array(data.get("encoding", []))
                            if encoding.size > 0:
                                self.encodings.append(encoding)
                                self.info.append({"id": sid, "name": data.get("name", "Unknown"), "roll_no": data.get("roll_no", "N/A")})
                logger.info(f"✅ Loaded {len(self.encodings)} students")
            except Exception as e:
                logger.error(f"Error loading encodings: {e}")


    def mark_attendance(self, student_id):
        current_time = time.time()
        with self.lock:
            if student_id in self.marked:
                return False
            last_time = self.last_seen.get(student_id, 0)
            if current_time - last_time < 3:
                return False
            self.last_seen[student_id] = current_time
            self.marked.add(student_id)
            return True


cache = AttendanceCache()


def get_attendance_stats(class_id, date=None):
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        attendance_data = db.reference(f"attendance/{date}/{class_id}").get()
        class_ref = db.reference(f'classes/{class_id}').get()
        total = len(class_ref.get('students', {})) if class_ref else 0
        present = len(attendance_data) if attendance_data else 0
        return {"total": total, "present": present, "absent": total - present, "percentage": round((present / total * 100), 2) if total > 0 else 0}
    except:
        return {"total": 0, "present": 0, "absent": 0, "percentage": 0}


def generate_qr_code(student_id):
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(f"STUDENT:{student_id}")
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return base64.b64encode(buffer.getvalue()).decode()
    except:
        return ""


@app.route('/')
def index():
    if 'logged_in' in session:
        if session.get('user_type') == 'teacher':
            return redirect(url_for('dashboard'))
        return redirect(url_for('student_dashboard'))
    return render_template('login.html')


@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    user_type = request.form.get('user_type', 'teacher')


    if user_type == 'teacher':
        admin_user = os.environ.get("ADMIN_USERNAME", "admin")
        admin_pass = os.environ.get("ADMIN_PASSWORD", "gla123")
        if username == admin_user and password == admin_pass:
            session['logged_in'] = True
            session['username'] = username
            session['user_type'] = 'teacher'
            return redirect(url_for('dashboard'))
    else:
        try:
            student = db.reference(f'students/{username}').get()
            if student and student.get('password') == password:
                session['logged_in'] = True
                session['username'] = username
                session['user_type'] = 'student'
                session['student_id'] = username
                return redirect(url_for('student_dashboard'))
        except:
            pass
    return render_template('login.html', error="Invalid credentials"), 401


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    try:
        classes_ref = db.reference('classes').get()
        classes = []
        total_students = 0
        total_present = 0
        if classes_ref:
            for class_id, class_data in classes_ref.items():
                student_count = len(class_data.get('students', {}))
                stats = get_attendance_stats(class_id)
                total_students += stats['total']
                total_present += stats['present']
                classes.append({"id": class_id, "name": class_data.get('name', 'Unknown'), "time": class_data.get('time', 'N/A'),
                              "room": class_data.get('room', 'N/A'), "student_count": student_count, 
                              "today_present": stats['present'], "today_percentage": stats['percentage']})
        overall_percentage = round((total_present / total_students * 100), 2) if total_students > 0 else 0
        return render_template('dashboard.html', classes=classes, total_students=total_students,
                             total_present=total_present, overall_percentage=overall_percentage, username=session.get('username'))
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return render_template('dashboard.html', classes=[], total_students=0, total_present=0, overall_percentage=0)


@app.route('/student/dashboard')
def student_dashboard():
    if 'logged_in' not in session or session.get('user_type') != 'student':
        return redirect(url_for('index'))
    try:
        student_id = session.get('student_id')
        student_data = db.reference(f'students/{student_id}').get()
        if not student_data:
            return "Student not found", 404
        attendance_history = []
        today = datetime.now(timezone.utc)
        class_id = student_data.get('class_id')
        present_days = 0
        for i in range(30):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            attendance = db.reference(f"attendance/{date}/{class_id}/{student_id}").get()
            status = "Present" if attendance else "Absent"
            if status == "Present":
                present_days += 1
            attendance_history.append({"date": date, "status": status, "time": attendance.get('time', '-') if attendance else '-'})
        attendance_percentage = round((present_days / 30 * 100), 2)
        qr_code = generate_qr_code(student_id)
        return render_template('student_dashboard.html', student=student_data, student_id=student_id,
                             attendance_history=attendance_history, present_days=present_days,
                             total_days=30, attendance_percentage=attendance_percentage, qr_code=qr_code)
    except Exception as e:
        logger.error(f"Student dashboard error: {e}")
        return "Error loading dashboard", 500


@app.route('/create_class', methods=['GET', 'POST'])
def create_class():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    if request.method == 'POST':
        try:
            class_id = request.form.get('class_id', '').strip().upper()
            class_name = request.form.get('class_name', '').strip()
            class_time = request.form.get('class_time', '').strip()
            room_number = request.form.get('room_number', '').strip()
            capacity = int(request.form.get('capacity', 50))
            if not all([class_id, class_name]):
                return jsonify({"status": "error", "message": "Missing fields"}), 400
            existing = db.reference(f'classes/{class_id}').get()
            if existing:
                return jsonify({"status": "error", "message": "Class exists"}), 400
            db.reference(f'classes/{class_id}').set({"name": class_name, "time": class_time, "room": room_number, 
                                                     "capacity": capacity, "created_at": datetime.now(timezone.utc).isoformat(),
                                                     "created_by": session.get('username'), "students": {}})
            logger.info(f"✅ Class {class_id} created")
            return jsonify({"status": "success", "message": f"Class {class_name} created"}), 200
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    return render_template('create_class.html')


@app.route('/manage_class/<class_id>')
def manage_class(class_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    try:
        class_data = db.reference(f'classes/{class_id}').get()
        if not class_data:
            return "Class not found", 404
        students = []
        students_ref = db.reference('students').get()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if students_ref:
            for sid, sdata in students_ref.items():
                if sdata.get('class_id') == class_id:
                    attendance_today = db.reference(f"attendance/{today}/{class_id}/{sid}").get()
                    total_classes = sdata.get('total_classes', 1)
                    attended = sdata.get('attended_classes', 0)
                    students.append({"id": sid, "name": sdata.get('name'), "roll_no": sdata.get('roll_no'),
                                   "email": sdata.get('email', 'N/A'), "present_today": "Yes" if attendance_today else "No",
                                   "attendance_percentage": round((attended / total_classes * 100), 2) if total_classes > 0 else 0})
        return render_template('manage_class.html', class_id=class_id, class_data=class_data, students=students)
    except Exception as e:
        logger.error(f"Error: {e}")
        return "Error loading class", 500


@app.route('/add_student_page/<class_id>')
def add_student_page(class_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    class_data = db.reference(f'classes/{class_id}').get()
    class_name = class_data.get('name', class_id) if class_data else class_id
    return render_template('add_student.html', class_id=class_id, class_name=class_name)


@app.route('/add_student', methods=['POST'])
def add_student():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    student_id = request.form.get('student_id', '').strip()
    name = request.form.get('name', '').strip()
    class_id = request.form.get('class_id', '').strip()
    roll_no = request.form.get('roll_no', '').strip()
    email = request.form.get('email', '').strip()
    if not all([student_id, name, class_id]):
        return jsonify({"status": "error", "message": "Missing required fields"}), 400
    existing = db.reference(f'students/{student_id}').get()
    if existing:
        return jsonify({"status": "error", "message": "Student ID already exists"}), 400
    try:
        cam = cv2.VideoCapture(0)
        if not cam.isOpened():
            return jsonify({"status": "error", "message": "Camera not accessible"}), 400
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        for _ in range(10):
            cam.read()
        frames = []
        for _ in range(5):
            success, frame = cam.read()
            if success:
                frames.append(frame)
            time.sleep(0.1)
        cam.release()
        if not frames:
            return jsonify({"status": "error", "message": "Failed to capture frames"}), 400
        all_encodings = []
        best_frame = None
        for frame in frames:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            encodings = face_recognition.face_encodings(rgb_frame)
            if encodings:
                all_encodings.append(encodings[0])
                if best_frame is None:
                    best_frame = frame
        if not all_encodings:
            return jsonify({"status": "error", "message": "No face detected. Please look at camera."}), 400
        avg_encoding = np.mean(all_encodings, axis=0).tolist()
        db.reference(f'students/{student_id}').set({"name": name, "class_id": class_id, "roll_no": roll_no, "email": email,
                                                    "encoding": avg_encoding, "password": student_id, "last_attendance": "Never",
                                                    "created_at": datetime.now(timezone.utc).isoformat(), "total_classes": 0, "attended_classes": 0})
        db.reference(f'classes/{class_id}/students/{student_id}').set(True)
        if bucket:
            blob = bucket.blob(f"student_faces/{student_id}.jpg")
            _, buffer = cv2.imencode('.jpg', best_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            blob.upload_from_string(buffer.tobytes(), content_type='image/jpeg')
        logger.info(f"✅ Student {name} added")
        return jsonify({"status": "success", "message": f"Student {name} added! Password: {student_id}"}), 200
    except Exception as e:
        logger.error(f"Error adding student: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/delete_student/<student_id>', methods=['POST', 'DELETE'])
def delete_student(student_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        student_data = db.reference(f'students/{student_id}').get()
        if not student_data:
            return jsonify({"status": "error", "message": "Student not found"}), 404
        class_id = student_data.get('class_id')
        db.reference(f'students/{student_id}').delete()
        db.reference(f'classes/{class_id}/students/{student_id}').delete()
        if bucket:
            try:
                blob = bucket.blob(f"student_faces/{student_id}.jpg")
                blob.delete()
            except:
                pass
        logger.info(f"✅ Student {student_id} deleted")
        return jsonify({"status": "success", "message": "Student deleted successfully"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/attendance/<class_id>')
def attendance(class_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    class_info = db.reference(f'classes/{class_id}').get()
    class_name = class_info.get('name', class_id) if class_info else class_id
    return render_template('attendance.html', class_id=class_id, class_name=class_name)


def gen_frames(class_id):
    camera = cv2.VideoCapture(0)
    if not camera.isOpened():
        return
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cache.load_encodings(class_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frame_count = 0
    last_locations = []
    last_names = []
    while True:
        success, frame = camera.read()
        if not success:
            break
        frame_count += 1
        if frame_count % 2 == 0 and cache.encodings:
            small = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            locs = face_recognition.face_locations(rgb, model='hog')
            encs = face_recognition.face_encodings(rgb, locs)
            last_locations = []
            last_names = []
            for enc, loc in zip(encs, locs):
                matches = face_recognition.compare_faces(cache.encodings, enc, tolerance=0.45)
                dists = face_recognition.face_distance(cache.encodings, enc)
                name = "Unknown"
                sid = None
                conf = 0
                if len(dists) > 0:
                    best = np.argmin(dists)
                    if matches[best]:
                        student = cache.info[best]
                        name = student["name"]
                        sid = student["id"]
                        conf = 1 - dists[best]
                        if cache.mark_attendance(sid):
                            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                            db.reference(f"attendance/{today}/{class_id}/{sid}").set({"name": name, "time": ts, "status": "Present", "confidence": float(conf)})
                            student_ref = db.reference(f'students/{sid}')
                            curr = student_ref.child('attended_classes').get() or 0
                            student_ref.update({"last_attendance": today, "attended_classes": curr + 1})
                            socketio.emit('new_attendance', {'name': name, 'id': sid, 'time': ts, 'confidence': f"{conf*100:.1f}%"})
                t, r, b, l = [v * 4 for v in loc]
                last_locations.append((t, r, b, l))
                last_names.append((name, conf))
        for (t, r, b, l), (name, conf) in zip(last_locations, last_names):
            color = (0, 200, 0) if name != "Unknown" else (0, 0, 255)
            cv2.rectangle(frame, (l, t), (r, b), color, 2)
            cv2.rectangle(frame, (l, b - 35), (r, b), color, cv2.FILLED)
            label = f"{name}" if name == "Unknown" else f"{name} ({conf*100:.0f}%)"
            cv2.putText(frame, label, (l + 6, b - 6), cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(frame, f"Students: {len(cache.encodings)} | Marked: {len(cache.marked)}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    camera.release()


@app.route('/video_feed/<class_id>')
def video_feed(class_id):
    if 'logged_in' not in session:
        return "Unauthorized", 401
    return Response(gen_frames(class_id), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/analytics')
def analytics():
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    classes_ref = db.reference('classes').get()
    classes = []
    if classes_ref:
        for class_id, class_data in classes_ref.items():
            classes.append({"id": class_id, "name": class_data.get('name', class_id)})
    return render_template('analytics.html', classes=classes)


@app.route('/api/ai-insights/<class_id>')
def ai_insights(class_id):
    if 'logged_in' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        stats = get_attendance_stats(class_id)
        if not gemini_client:
            insights = f"""📊 Attendance Analysis:


1. Room Utilization: {stats['percentage']}% attendance - {'✓ Optimal' if 70 <= stats['percentage'] <= 90 else '⚠ Needs attention'}


2. Today's Status: {stats['present']} out of {stats['total']} students present


3. Recommendation: {'Continue current engagement' if stats['percentage'] > 75 else 'Implement attendance reminders'}"""
            return jsonify({"status": "success", "insights": insights, "stats": stats, "ai_enabled": False})
        prompt = f"""Analyze attendance for {class_id}: {stats['present']}/{stats['total']} present ({stats['percentage']}%). Give 3 brief insights (20 words each): 1) Room utilization 2) Pattern 3) Recommendation"""
        response = gemini_client.generate_content(prompt)
        return jsonify({"status": "success", "insights": response.text, "stats": stats, "ai_enabled": True})
    except Exception as e:
        stats = get_attendance_stats(class_id)
        return jsonify({"status": "success", "insights": f"Attendance: {stats['present']}/{stats['total']} ({stats['percentage']}%)", "stats": stats, "ai_enabled": False})


@app.route('/api/room-analytics')
def room_analytics():
    if 'logged_in' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        classes_ref = db.reference('classes').get()
        analytics = []
        if classes_ref:
            for cid, cdata in classes_ref.items():
                stats = get_attendance_stats(cid)
                util = stats['percentage']
                analytics.append({"class_id": cid, "class_name": cdata.get('name'), "capacity": stats['total'],
                                "present": stats['present'], "utilization": util,
                                "status": "optimal" if 70 <= util <= 90 else "underutilized" if util < 70 else "overcrowded"})
        avg = round(sum(r['utilization'] for r in analytics) / len(analytics), 1) if analytics else 0
        return jsonify({"date": today, "rooms": analytics, "summary": {"total_rooms": len(analytics), "avg_utilization": avg}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/manual_attendance/<class_id>', methods=['GET', 'POST'])
def manual_attendance(class_id):
    if 'logged_in' not in session or session.get('user_type') != 'teacher':
        return redirect(url_for('index'))
    if request.method == 'POST':
        try:
            sid = request.form.get('student_id')
            action = request.form.get('action')
            reason = request.form.get('reason', 'Manual correction')
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            sdata = db.reference(f'students/{sid}').get()
            if not sdata:
                return jsonify({"error": "Student not found"}), 404
            if action == 'mark_present':
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                db.reference(f"attendance/{today}/{class_id}/{sid}").set({"name": sdata['name'], "time": ts, "status": "Present",
                                                                          "manual": True, "reason": reason, "marked_by": session.get('username')})
                return jsonify({"status": "success", "message": "Marked present"})
            elif action == 'mark_absent':
                db.reference(f"attendance/{today}/{class_id}/{sid}").delete()
                return jsonify({"status": "success", "message": "Marked absent"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    students = []
    students_ref = db.reference('students').get()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if students_ref:
        for sid, sdata in students_ref.items():
            if sdata.get('class_id') == class_id:
                att = db.reference(f"attendance/{today}/{class_id}/{sid}").get()
                students.append({"id": sid, "name": sdata.get('name'), "roll_no": sdata.get('roll_no'),
                               "status": "Present" if att else "Absent", "manual": att.get('manual', False) if att else False})
    return render_template('manual_attendance.html', class_id=class_id, students=students)


@app.route('/report/<class_id>')
def attendance_report(class_id):
    if 'logged_in' not in session:
        return redirect(url_for('index'))
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        att_data = db.reference(f"attendance/{today}/{class_id}").get()
        report = []
        if att_data:
            for sid, data in att_data.items():
                report.append({"id": sid, "name": data.get("name"), "roll_no": db.reference(f'students/{sid}/roll_no').get(),
                             "time": data.get("time"), "status": data.get("status"), "confidence": data.get("confidence", "Manual"),
                             "manual": data.get("manual", False)})
        return render_template('report.html', report=report, class_id=class_id, date=today)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@socketio.on('connect')
def handle_connect():
    emit('connection_response', {'status': 'Connected'})


@socketio.on('disconnect')
def handle_disconnect():
    pass


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


if __name__ == '__main__':
    logger.info("🚀 Smart Attendance System Starting...")
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=os.environ.get('FLASK_DEBUG', 'False') == 'True', host='0.0.0.0', port=port, use_reloader=False)
so this is app.py file 
