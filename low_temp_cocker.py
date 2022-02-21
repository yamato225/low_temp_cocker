import concurrent.futures
import time
import os
import re
import pigpio
from multiprocessing import Process, Value
import sys

from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError

# Defines
ONEWIRE_PATH="/sys/bus/w1/devices"
#SENSOR_LABELS={"28-3c01a8169133":"heater","28-3c01a816d9f0":"water","28-3c01d607f380":"heater2","28-3c01d60747d7":"heater3","28-3c01d6076b83":"heater4","28-3c01d607efb1":"heater5"}
SENSOR_LABELS={"28-3c01a816d9f0":"water","28-3c01d607f380":"heater2","28-3c01d60747d7":"heater3","28-3c01d6076b83":"heater4","28-3c01d607efb1":"heater5"}
#SENSOR_LABELS={"28-3c01d607f380":"heater2","28-3c01d60747d7":"heater3","28-3c01d6076b83":"heater4","28-3c01d607efb1":"water"}
SENSOR_ADJ={"heater4":17}
GPIO_PULSE1=16
GPIO_ENABLE1=12
TARGET_TEMP=57
AVG_NUM=5
MAX_TIME=12
HEATER_TEST_DURATION=30
HEATER_ERROR_THRESHOLD=20
MAX_DIFF_THRESHOLD=3
HEATER_MAX_TEMP=80
#環境変数取得
YOUR_CHANNEL_ACCESS_TOKEN = os.environ["YOUR_CHANNEL_ACCESS_TOKEN"]
LINE_NOTICE_TARGET = os.environ["LINE_NOTICE_TARGET"]

def read_temp_file(file_name):
    ##
    # @brief 指定された1-wire温度センサの値を取得する。
    # @param file_name デバイス名(/sys/bus/w1/devices以下のディレクトリ名)
    with open(ONEWIRE_PATH+"/"+file_name+"/w1_slave") as f:
        temp=0
        try:
            temp=int(re.findall('.*t=([0-9]+)$',f.read()).pop())/1000
        except Exception as exc:
            #print(str(exc))
            temp=0
        return temp

def get_temp_list(labels):
    ##
    # @brief 温度取得関数
    # @details 指定された1-wire温度センサの値を取得する。
    # @param labels 1-wireデバイス名をキー、そのセンサーの値の名称を値としたdict
    result={}
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_list={executor.submit(read_temp_file,dev_name): dev_name for dev_name in list(labels.keys())}
        for future in concurrent.futures.as_completed(future_list):
            dev_name=future_list[future]
            try:
                result[labels[dev_name]]=future.result()
            except Exception as exc:
                pass

    return result

def control_heater(st: Value):
    ##
    # @brief ヒーター制御関数
    # @details 立ち下がりパルスを与えてヒーターを作動させる。
    # @param st ヒーター作動時間(0.01秒単位)
    pulse=0
    pi=pigpio.pi()

    pi.set_mode(GPIO_ENABLE1,pigpio.OUTPUT)
    pi.set_mode(GPIO_PULSE1,pigpio.OUTPUT)
    pi.write(GPIO_ENABLE1,1)
    while True:
        if st.value > 0:
            # ヒーターを指定された時間だけ作動。
            st.value -= 1
            pulse=1-pulse
            # 0.01秒毎に立ち下がりパルスを与える。
            pi.write(GPIO_PULSE1,pulse)
        time.sleep(0.01)

def monitor_temp(st: Value):
    ##
    # @brief 温度監視関数(プロセス)
    # @details 温度を監視しヒータを操作する。
    # @param st ヒーター作動時間(0.01秒単位)

    zero_count=0

    # 監視開始時刻を確認する。
    start_time=time.time()
    last_time=start_time
    ontime=0
    total_time=0
    temp_diff=0
    max_temp_diff=0
    sleep_time=0
    current_time=0

    #平均温度
    avg_temp=0.0
    temp_array=list()
    old_temp_list={}
    heater_array=list()

    # LINE BOT初期化
    line_bot_api=LineBotApi(YOUR_CHANNEL_ACCESS_TOKEN)

    try:
        #pass
        line_bot_api.push_message(LINE_NOTICE_TARGET, TextSendMessage(text='加熱開始'))
    except LineBotApi as e:
        print("Failed to initialize LINE API")
        return -1

    t=0
    is_noticed=False
    is_emerg=False
    while True:
        temp_list=get_temp_list(SENSOR_LABELS)
        # 正常処理
        wt=temp_list['water']
        #if t==0:
        #    correct_data=list(filter(lambda x: x > 0, temp_list.values()))
        #    wt=sum(correct_data)/len(correct_data)
        if wt>0:
            temp_array.append(wt)
        h2t=temp_list['heater2']
        if h2t>0 and h2t<TARGET_TEMP:
           temp_array.append(h2t)
        if len(temp_array)>AVG_NUM:
            temp_array.pop(0)
        if len(temp_array)>0:
            avg_temp=sum(temp_array)/len(temp_array)
        if avg_temp<TARGET_TEMP:
            t=300
        else:
            t=0
        # 異常処理
        ## 温度取得に10回連続で失敗したら終了
        if len([x for x in temp_list.values() if x < 1])>0:
            zero_count+=1
            if zero_count>10:
                t=0
                time.sleep(5)
            if zero_count>30:
                msg="センサー異常発生"
                break
        else:
            zero_count=0

        max_temp_diff=0
        for key in temp_list.keys():
            if key in SENSOR_ADJ.keys():
                adj_temp=SENSOR_ADJ[key]
            else:
                adj_temp=0
            if temp_list[key] - adj_temp > HEATER_MAX_TEMP:
                max_temp=temp_list[key]
                is_emerg=True
                break
            try:
                temp_diff=temp_list[key] - old_temp_list[key]
                #print(""+key+",diff="+str(temp_diff)+"     "+str(temp_list[key])+","+str(old_temp_list[key]))
            except:
                temp_diff=0
            if temp_diff > max_temp_diff:
                max_temp_diff=temp_diff
            if temp_list[key]>0:
                old_temp_list[key]=temp_list[key]
            if temp_diff>MAX_DIFF_THRESHOLD:
                # n度上昇したら、n秒待つ。
                sleep_time=temp_diff
                break

        if is_emerg:
            msg="異常加熱発生"
            print(msg+"temp="+str(max_temp))
            break
        if current_time>0:
            time_diff=time.time() - current_time
        else:
            time_diff=0
        current_time=time.time()
        if sleep_time>0:
            t=0
            sleep_time-=time_diff
        temp_ratio=max(temp_list.values())/avg_temp
        st.value=t
        ## 開始からMAX_TIME時間経過したら終了
        total_time=current_time-start_time
        if total_time > MAX_TIME*3600:
            msg="時間切れ"
            break
        if t>0:
            ontime+=current_time - last_time
        last_time=current_time
        temp_msg=str(sorted(temp_list.items(),key=lambda x:x[0]))
        #if (round(total_time,0) % 600)==0:
        #    line_bot_api.push_message(LINE_NOTICE_TARGET, TextSendMessage(text='水温:'+str(avg_temp)))

        if total_time>30 and is_noticed == False and avg_temp >= TARGET_TEMP:
            line_bot_api.push_message(LINE_NOTICE_TARGET, TextSendMessage(text='お風呂が沸きました。'))
            is_noticed=True
        # 動作状態表示
        print(str(round(avg_temp,1))+" run:"+str(round(total_time/60,1))+" on:"+str(round(ontime/60,1))+",st="+str(st.value)+",sleep="+str(round(sleep_time)) )
        print(temp_msg)
        time.sleep(0.5)

    print(msg)
    line_bot_api.push_message(LINE_NOTICE_TARGET, TextSendMessage(text=msg))
    return -1


def main():
    ##
    # @brief メイン関数
    # @details ヒーター制御関数と温度監視関数を別プロセスで起動する。
    shared_time = Value('i', 0)
    control_process=Process(target=control_heater,args=(shared_time,))
    control_process.start()
    monitor_process=Process(target=monitor_temp,args=(shared_time,))
    monitor_process.start()
    sys.exit(0)



if __name__ == "__main__":
    if(len(sys.argv)>=2):
        time.sleep(int(sys.argv[1]))
    main()
