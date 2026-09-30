# Sweep Test Scripts

**`backend_sweep_test.py`: frequency comparison.** This test compares the
433 MHz and 446 MHz channels to see which one gives more reliable
communication between the sensor boards. It tries each channel at several
signal strengths and records how many messages arrive and how clean the signal
is. We use the results to choose the operating frequency that loses the
fewest messages in our real environment.

**`mdbpower_sweep_test.py`: transmit power comparison.** This test keeps the
frequency fixed and steps the boards' transmit power from the lowest to the
highest setting. It shows how much power the boards actually need for
reliable communication. With that, we can choose a power level that keeps
messages getting through without wasting energy, keeps battery life long,
reduces interference with nearby equipment and stays within regulatory limits.
