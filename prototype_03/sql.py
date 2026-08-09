def measurement_in_range(start_ts, end_ts):
    query = f"""
        SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
            AND pc_ts >= '{start_ts}'
            AND pc_ts <= '{end_ts}'
            AND sender_id IS NOT NULL
            AND receiver_id IS NOT NULL
            AND sender_id != receiver_id
        ORDER BY pc_ts ASC
        """
    return query

def measurement_for_many_days(first_start_ts, last_end_ts, start_hhmm, end_hhmm):
    return f"""
        SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
            AND pc_ts >= '{first_start_ts}'
            AND pc_ts <= '{last_end_ts}'
            AND substr(pc_ts, 12, 5) >= '{start_hhmm}'
            AND substr(pc_ts, 12, 5) <= '{end_hhmm}'
            AND sender_id IS NOT NULL
            AND receiver_id IS NOT NULL
            AND sender_id != receiver_id
        ORDER BY pc_ts ASC
    """
