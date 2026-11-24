// A first-order decay with an independently known solution y(t) = exp(-2t).
function derivative = decayRate(t, y)
    derivative = -2 * y;
endfunction

times = 0:0.05:2;
numerical = ode(1, 0, times, 1e-10, 1e-11, decayRate);
exact = exp(-2 * times);
csvWrite([times', numerical', exact'], "decay.csv");
exit();
