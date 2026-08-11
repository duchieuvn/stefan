def measurement_in_range(start_ts, end_ts):
    query = """
        SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
            AND pc_ts >= %s
            AND pc_ts <= %s
            AND sender_id IS NOT NULL
            AND receiver_id IS NOT NULL
            AND sender_id != receiver_id
        ORDER BY pc_ts ASC
        """
    params = (start_ts, end_ts)
    return query, params
