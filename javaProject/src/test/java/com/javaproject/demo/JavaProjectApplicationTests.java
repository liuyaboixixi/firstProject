package com.javaproject.demo;

import com.javaproject.demo.mapper.UserMapper;
import com.javaproject.demo.model.User;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.junit4.SpringRunner;

@RunWith(SpringRunner.class)
@SpringBootTest
public class JavaProjectApplicationTests {
    @Autowired
    private UserMapper userMapper;
    User user = new User();
    @Test
    public void contextLoads() {

   userMapper.insert(user);

    }

}
